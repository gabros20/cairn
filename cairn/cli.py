"""cairn CLI — the argparse frame plus the thin wiring that turns the kernel into a tool.

Every subcommand parses its flags, wires kernel objects together, and prints; the logic
stays in the kernel. Verbs (docs/API.md §9): plan/run/resume/gate/validate/trail/ps/doctor/
test/compose/new are live; batch/learnings/gc/schedule remain stubs (exit 2).

Guard wiring (the pinned contract): in run/resume, a plan's ``shim``-enforced guards get a
fresh PATH-shim dir per run (:func:`~cairn.kernel.guards.build_shims`), and every executor is
wrapped in a :class:`GuardedExecutor` that PREPENDS the shim dir to each invocation's PATH.
Hook-layer wiring is C4 (executors' ``install_guards`` is still a documented no-op).
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import datetime
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import cairn
from cairn.kernel import doctor as doctor_mod
from cairn.kernel import newkit
from cairn.kernel.artifacts import resolve_path, validate
from cairn.kernel.compose import make_composer, render_artifact_path
from cairn.kernel.config import Config, ExecutorConfig, load_config
from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.gatekit import answer_gate, is_answered, read_choice
from cairn.kernel.guards import build_shims
from cairn.kernel.plan import (
    GateNode,
    LoopNode,
    ParallelNode,
    Plan,
    StepNode,
    plan as build_plan,
    render_run_id,
)
from cairn.kernel.runstate import load_run, update_run
from cairn.kernel.trail import derive_status, follow, read_trail
from cairn.kernel.types import ExitCode
from cairn.kernel.walk import bootstrap_run, walk

# The full verb surface (docs/API.md §9). Order is the help-listing order.
SUBCOMMANDS: list[str] = [
    "plan",
    "run",
    "resume",
    "gate",
    "validate",
    "trail",
    "ps",
    "doctor",
    "test",
    "new",
    "compose",
    "batch",
    "learnings",
    "gc",
    "schedule",
]

_STUB_VERBS = {"batch", "learnings", "gc", "schedule"}


# --------------------------------------------------------------------------- #
# Small shared helpers.
# --------------------------------------------------------------------------- #


def _now() -> datetime:
    return datetime.now()


def _workspace(args: argparse.Namespace) -> Path:
    # Absolute: the kernel writes absolute paths into envelopes/commands, and a run dir under
    # an absolute workspace is itself absolute (a relative run dir breaks cwd-relative steps).
    return Path(getattr(args, "workspace", None) or ".").resolve()


def _kv(pairs: list[str] | None) -> dict[str, str]:
    """Parse ``["k=v", ...]`` into a dict; a missing ``=`` is a usage error."""
    out: dict[str, str] = {}
    for item in pairs or []:
        if "=" not in item:
            raise _Usage(f"expected key=value, got {item!r}")
        key, _, value = item.partition("=")
        out[key] = value
    return out


class _Usage(Exception):
    """A malformed argument the parser couldn't catch (→ exit 2 with the message)."""


def _pipeline_hash(workspace_dir: Path, pipeline: str) -> str:
    f = Path(workspace_dir) / "pipelines" / f"{pipeline}.yaml"
    return "sha256:" + hashlib.sha256(f.read_bytes()).hexdigest()


def _runs_root(workspace_dir: Path, config) -> Path:
    root = Path(config.workspace.runs_dir)
    return root if root.is_absolute() else Path(workspace_dir) / root


# --------------------------------------------------------------------------- #
# Executor discovery + construction — the ``cairn.executors`` entry-point registry.
# --------------------------------------------------------------------------- #


def load_executor_class(name: str) -> type:
    """Load the executor class registered under ``name``; KeyError if none is registered."""
    for ep in entry_points(group="cairn.executors"):
        if ep.name == name:
            return ep.load()
    raise KeyError(name)


def build_executor(name: str, cls: type, ec: ExecutorConfig | None) -> Any:
    """Instantiate ``cls`` with its ``ExecutorConfig`` (positional for every executor).

    A missing table is synthesized as an empty ``ExecutorConfig`` so the default executor can
    be built even before the workspace declares tiers (the day-0 ``hello`` case)."""
    return cls(ec if ec is not None else ExecutorConfig(name=name))


def build_executors(resolved_models: dict, config: Config) -> dict[str, Any]:
    """Build every executor a plan needs: each in ``resolved_models`` + always ``shell``.

    An executor named in ``cairn.toml`` (so it planned fine) but with no registered
    ``cairn.executors`` plugin raises a typed :class:`CairnError` — mapped to ExitCode.EXECUTOR
    by the callers — rather than a raw ``KeyError`` traceback."""
    names = {exec_name for exec_name, _m, _e in resolved_models.values()}
    names.add("shell")
    out: dict[str, Any] = {}
    for name in names:
        try:
            cls = load_executor_class(name)
        except KeyError as exc:
            raise CairnError(
                f"executor {name!r} has no registered plugin (no such executor plugin) — "
                f"check the cairn.executors entry points"
            ) from exc
        out[name] = build_executor(name, cls, config.executors.get(name))
    return out


def _print_config_error(exc: ConfigError) -> int:
    where = f"{exc.file}: " if exc.file else ""
    if exc.line is not None:
        where = f"{exc.file}:{exc.line}: "
    print(f"cairn: {where}{exc}", file=sys.stderr)
    for f in exc.findings:
        if f.message != str(exc):
            print(f"  - {f.message}", file=sys.stderr)
    return int(ExitCode.CONFIG)


def _param_str(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def _iter_steps(nodes, in_loop: bool = False):
    for node in nodes:
        if isinstance(node, StepNode):
            yield node, in_loop
        elif isinstance(node, ParallelNode):
            yield from _iter_steps(node.steps, in_loop)
        elif isinstance(node, LoopNode):
            yield from _iter_steps(node.body, True)


# --------------------------------------------------------------------------- #
# Guard wiring (the reviewers' pinned contract).
# --------------------------------------------------------------------------- #


class GuardedExecutor:
    """Wraps an executor so every invocation runs with the run's guard shims on PATH.

    Delegates the whole Executor protocol to ``inner`` except :meth:`invoke`, which PREPENDS
    the shim dir to ``inv.env['PATH']`` (append would be a total bypass) and exports the
    shim-dir/manifest env the shims read. Hook-layer wiring is C4 — ``install_guards`` is
    still a documented no-op on the inner executors.
    """

    def __init__(self, inner: Any, delta: dict[str, str], shim_dir: Path) -> None:
        self._inner = inner
        self._delta = delta
        self._shim_dir = Path(shim_dir)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def invoke(self, inv):
        env = {
            **inv.env,
            "PATH": f"{self._delta['PATH']}:{inv.env.get('PATH', '')}",
            "CAIRN_SHIM_DIR": self._delta["CAIRN_SHIM_DIR"],
            "CAIRN_SHIM_MANIFEST": str(self._shim_dir / "manifest.json"),
        }
        return self._inner.invoke(dataclasses.replace(inv, env=env))


def _wrap_guards(plan: Plan, executors: dict[str, Any], run_dir: Path, workspace_dir: Path) -> dict[str, Any]:
    """If the plan has ``shim``-enforced guards, build a FRESH per-run shim dir and wrap every
    executor. No shim guards → the executors are returned unchanged."""
    shim_guards = [g for g in plan.guards if "shim" in g.enforce]
    if not shim_guards:
        return executors
    shim_dir = Path(run_dir) / ".cairn" / "shims"
    shutil.rmtree(shim_dir, ignore_errors=True)  # fresh per run (contract; engine also sweeps)
    delta = build_shims(shim_guards, shim_dir=shim_dir, workspace_dir=Path(workspace_dir))
    if not delta:
        return executors
    return {name: GuardedExecutor(ex, delta, shim_dir) for name, ex in executors.items()}


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #


def _model_str(plan: Plan, step_id: str) -> str:
    exec_name, model, effort = plan.resolved_models[step_id]
    m = f"{model}/{effort}" if effort else model
    return f"{exec_name}·{m}"


def _cmd_plan(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    try:
        p = build_plan(
            ws,
            args.pipeline,
            _kv(args.param),
            executor=args.executor,
            now=_now(),
            to_node=args.to,
            from_node=args.from_node,
        )
    except ConfigError as exc:
        return _print_config_error(exc)
    if args.json:
        print(json.dumps(_plan_json(p), indent=2))
        return int(ExitCode.OK)
    _print_plan(p)
    return int(ExitCode.OK)


def _print_plan(p: Plan) -> None:
    print(f"{p.pipeline} (v{p.version})")
    if p.params:
        print("  params: " + ", ".join(f"{k}={v}" for k, v in p.params.items()))
    if p.dims:
        print("  dims:   " + ", ".join(f"{k}={v}" for k, v in p.dims.items()))
    print("  nodes:")
    for node in p.nodes:
        _print_node(p, node, indent=4)
    if p.skipped:
        print("  plan-skipped:")
        for s in p.skipped:
            print(f"    ─ {s.node} ({s.kind}) — {s.reason}")
    for w in p.warnings:
        print(f"  ! {w.message}")


def _print_node(p: Plan, node, indent: int) -> None:
    pad = " " * indent
    if isinstance(node, StepNode):
        if node.kind == "agent":
            head = f"{pad}• {node.id} [agent {node.agent.name} · {_model_str(p, node.id)}]"
        else:
            head = f"{pad}• {node.id} [{node.kind}]"
        flow = _flow(node)
        print(head + (f"  {flow}" if flow else ""))
    elif isinstance(node, GateNode):
        opts = "/".join(k for k, _ in node.options)
        print(f"{pad}◆ gate {node.name} [{opts}] default={node.default}")
    elif isinstance(node, ParallelNode):
        print(f"{pad}∥ parallel {node.name} (on_fail={node.on_fail})")
        for child in node.steps:
            _print_node(p, child, indent + 2)
    elif isinstance(node, LoopNode):
        until = f" until {node.until.source}" if node.until is not None else ""
        print(
            f"{pad}↻ loop {node.name} (min={node.min}, "
            f"max i={node.max_interactive}/h={node.max_headless}, on_cap={node.on_cap}){until}"
        )
        for child in node.body:
            _print_node(p, child, indent + 2)


def _flow(node: StepNode) -> str:
    parts = []
    if node.needs:
        parts.append("needs " + ",".join(node.needs))
    if node.needs_optional:
        parts.append("needs? " + ",".join(node.needs_optional))
    if node.produces:
        parts.append("→ " + ",".join(node.produces))
    return "  ".join(parts)


def _plan_json(p: Plan) -> dict:
    return {
        "pipeline": p.pipeline,
        "version": p.version,
        "params": p.params,
        "dims": p.dims,
        "run_id_template": p.run_id_template,
        "nodes": [_node_json(p, n) for n in p.nodes],
        "models": {sid: {"executor": e, "model": m, "effort": eff} for sid, (e, m, eff) in p.resolved_models.items()},
        "skipped": [{"node": s.node, "kind": s.kind, "reason": s.reason} for s in p.skipped],
        "warnings": [w.message for w in p.warnings],
    }


def _node_json(p: Plan, node) -> dict:
    if isinstance(node, StepNode):
        d = {
            "kind": node.kind,
            "id": node.id,
            "needs": list(node.needs),
            "needs_optional": list(node.needs_optional),
            "produces": list(node.produces),
        }
        if node.kind == "agent":
            e, m, eff = p.resolved_models[node.id]
            d["agent"] = node.agent.name
            d["executor"], d["model"], d["effort"] = e, m, eff
        return d
    if isinstance(node, GateNode):
        return {"kind": "gate", "name": node.name, "options": [k for k, _ in node.options], "default": node.default}
    if isinstance(node, ParallelNode):
        return {"kind": "parallel", "name": node.name, "on_fail": node.on_fail, "steps": [_node_json(p, c) for c in node.steps]}
    if isinstance(node, LoopNode):
        return {
            "kind": "loop",
            "name": node.name,
            "min": node.min,
            "max_interactive": node.max_interactive,
            "max_headless": node.max_headless,
            "on_cap": node.on_cap,
            "until": node.until.source if node.until is not None else None,
            "body": [_node_json(p, c) for c in node.body],
        }
    return {}


# --------------------------------------------------------------------------- #
# run / resume — the shared walk driver.
# --------------------------------------------------------------------------- #


def _drive(
    p: Plan,
    run_dir: Path,
    workspace_dir: Path,
    config,
    *,
    interactive: bool,
    gate_presets: dict[str, str],
    now: datetime,
    stub_mode: bool = False,
) -> int:
    if stub_mode:
        from cairn.executors.shell import ShellExecutor
        from cairn.executors.stub import StubExecutor

        stub = StubExecutor()
        executors: dict[str, Any] = {"shell": ShellExecutor(), "stub": stub}
        for exec_name, _m, _e in p.resolved_models.values():
            executors[exec_name] = stub
    else:
        executors = build_executors(p.resolved_models, config)
    executors = _wrap_guards(p, executors, run_dir, workspace_dir)
    composer = make_composer(workspace_dir=workspace_dir, config=config, now=now)
    code = walk(
        p,
        run_dir,
        workspace_dir=workspace_dir,
        config=config,
        executors=executors,
        composer=composer,
        interactive=interactive,
        gate_presets=gate_presets,
        now=now,
    )
    _print_walk_result(code, run_dir)
    return int(code)


def _print_walk_result(code: ExitCode, run_dir: Path) -> None:
    if code == ExitCode.OK:
        print(f"cairn: run complete → {run_dir}")
    elif code == ExitCode.NEEDS_HUMAN:
        print(f"cairn: halted awaiting a human → {run_dir}  (answer + `cairn resume {run_dir}`)", file=sys.stderr)
    else:
        print(f"cairn: run halted (exit {int(code)}) → {run_dir}", file=sys.stderr)


def _cmd_run(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    now = _now()
    stub_mode = args.executor == "stub"
    try:
        p = build_plan(
            ws,
            args.pipeline,
            _kv(args.param),
            executor=None if stub_mode else args.executor,
            step_executors=_kv(args.step_executor),
            now=now,
            to_node=args.to,
            from_node=args.from_node,
            headless=args.headless,
        )
        config = load_config(ws)
    except ConfigError as exc:
        return _print_config_error(exc)

    phash = _pipeline_hash(ws, args.pipeline)

    # Resolve the run dir: honor --run-dir, respect --idempotent (resume-or-no-op).
    created = False
    if args.run_dir:
        run_dir = Path(args.run_dir).resolve()
        existing = (run_dir / "run.json").is_file()
        if existing and args.idempotent:
            fast = _idempotent_shortcut(run_dir)
            if fast is not None:
                return fast
        elif not existing:
            run_dir = bootstrap_run(ws, p, now=now, run_dir=run_dir, pipeline_hash=phash)
            created = True
        # existing without --idempotent → resume the given dir as-is.
    else:
        runs_root = _runs_root(ws, config)
        if args.idempotent:
            candidate = runs_root / render_run_id(p, now)
            if (candidate / "run.json").is_file():
                fast = _idempotent_shortcut(candidate)
                if fast is not None:
                    return fast
                run_dir = candidate
            else:
                run_dir = bootstrap_run(ws, p, now=now, runs_root=runs_root, pipeline_hash=phash)
                created = True
        else:
            run_dir = bootstrap_run(ws, p, now=now, runs_root=runs_root, pipeline_hash=phash)
            created = True

    if created:
        # Record the actual executor routing so `cairn resume` reconstructs the same fleet
        # (mixed --executor / --step-executor) instead of silently falling back to defaults.
        global_default = (None if stub_mode else args.executor) or config.workspace.default_executor or ""
        _record_executor_routing(run_dir, now, global_default, _kv(args.step_executor))

    interactive = (not args.headless) and sys.stdin.isatty()
    try:
        return _drive(
            p, run_dir, ws, config,
            interactive=interactive, gate_presets=_kv(args.gate), now=now, stub_mode=stub_mode,
        )
    except CairnError as exc:
        print(f"cairn: {exc}", file=sys.stderr)
        return int(ExitCode.EXECUTOR)


def _record_executor_routing(run_dir: Path, now: datetime, default: str, overrides: dict[str, str]) -> None:
    """Persist the run's executor fleet into run.json (schema §8.1 ``executors``) so resume is
    faithful. ``default`` is the effective global executor; ``overrides`` the ``--step-executor``
    map. Versions stay empty (recording them is a cheap future add)."""

    def mutate(doc: dict) -> None:
        ex = doc.setdefault("executors", {})
        ex["default"] = default
        ex["overrides"] = dict(overrides)
        ex.setdefault("versions", {})

    update_run(run_dir, mutate)


def _idempotent_shortcut(run_dir: Path) -> int | None:
    """For ``--idempotent`` on an existing run dir: exit 0 fast if already done, else None
    (the caller resumes)."""
    try:
        status = load_run(run_dir).get("status")
    except (OSError, ValueError, ConfigError):
        return None
    if status == "done":
        print(f"cairn: already done → {run_dir}")
        return int(ExitCode.OK)
    return None


def _cmd_resume(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    ws = _workspace(args)
    now = _now()
    try:
        run_doc = load_run(run_dir)
    except (OSError, ValueError, ConfigError) as exc:
        print(f"cairn: cannot read {run_dir}/run.json: {exc}", file=sys.stderr)
        return int(ExitCode.CONFIG)

    pipeline = run_doc["pipeline"]
    params = {k: _param_str(v) for k, v in (run_doc.get("params") or {}).items() if v is not None}

    pfile = ws / "pipelines" / f"{pipeline}.yaml"
    if not pfile.is_file():
        print(f"cairn: cannot resume — pipeline file {pfile} no longer exists", file=sys.stderr)
        return int(ExitCode.CONFIG)

    current_hash = _pipeline_hash(ws, pipeline)
    recorded = run_doc.get("pipeline_hash")
    if recorded and recorded not in ("sha256:unknown", current_hash):
        if not args.force:
            print(
                f"cairn: pipeline {pipeline!r} has changed since this run was planned "
                f"(hash drift). Re-run with --force to resume against the current file.",
                file=sys.stderr,
            )
            return int(ExitCode.CONFIG)
        print(f"cairn: warning — resuming across pipeline-hash drift (--force) for {pipeline!r}", file=sys.stderr)

    # Reconstruct the recorded fleet (§8.1 executors) so a mixed-executor run resumes on the
    # same models, not the workspace defaults.
    ex_doc = run_doc.get("executors") or {}
    recorded_default = ex_doc.get("default") or None
    recorded_overrides = {k: str(v) for k, v in (ex_doc.get("overrides") or {}).items()}
    try:
        p = build_plan(ws, pipeline, params, executor=recorded_default, step_executors=recorded_overrides, now=now)
        config = load_config(ws)
    except ConfigError as exc:
        return _print_config_error(exc)

    interactive = sys.stdin.isatty()
    try:
        return _drive(p, run_dir, ws, config, interactive=interactive, gate_presets={}, now=now)
    except CairnError as exc:
        print(f"cairn: {exc}", file=sys.stderr)
        return int(ExitCode.EXECUTOR)


# --------------------------------------------------------------------------- #
# gate
# --------------------------------------------------------------------------- #


def _cmd_gate(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    ws = _workspace(args)
    spec = args.assignment
    if "=" not in spec:
        print(f"cairn: expected <name>=<choice>, got {spec!r}", file=sys.stderr)
        return int(ExitCode.CONFIG)
    name, _, choice = spec.partition("=")

    # (a) it must be a real run dir.
    try:
        run_doc = load_run(run_dir)
    except (OSError, ValueError, ConfigError):
        print(f"cairn: {run_dir} is not a run dir (no readable run.json)", file=sys.stderr)
        return int(ExitCode.CONFIG)

    # Re-plan from the recorded pipeline+params (same path resume uses) so we validate against
    # the run's actual gates — never blind-write a typo'd gate/choice.
    pipeline = run_doc["pipeline"]
    params = {k: _param_str(v) for k, v in (run_doc.get("params") or {}).items() if v is not None}
    now = _parse_created_at(run_doc.get("created_at"))
    try:
        p = build_plan(ws, pipeline, params, now=now)
    except ConfigError as exc:
        return _print_config_error(exc)

    gates = {g.name: g for g in _iter_gates(p.nodes)}
    gate = gates.get(name)
    if gate is None:
        skipped = {s.node for s in p.skipped if s.kind == "gate"}
        if name in skipped:
            print(f"cairn: gate {name!r} is not active in this run (its condition is false)", file=sys.stderr)
        else:
            known = ", ".join(sorted(gates)) or "(none)"
            print(f"cairn: no gate {name!r} in pipeline {pipeline!r} (gates: {known})", file=sys.stderr)
        return int(ExitCode.CONFIG)

    # (b) refuse to overwrite an already-recorded decision — explicit beats silent clobber.
    if is_answered(run_dir, name):
        print(f"cairn: gate {name!r} is already answered ({read_choice(run_dir, name)!r}); refusing to overwrite", file=sys.stderr)
        return int(ExitCode.CONFIG)

    # (c) the choice must be one of the gate's declared options.
    options = [k for k, _ in gate.options]
    if choice not in options:
        print(f"cairn: {choice!r} is not an option for gate {name!r} (options: {', '.join(options)})", file=sys.stderr)
        return int(ExitCode.CONFIG)

    answer_gate(run_dir, name, choice)
    print(f"cairn: gate {name!r} answered {choice!r} (by external) → {run_dir}")
    return int(ExitCode.OK)


def _iter_gates(nodes):
    """Yield every :class:`GateNode` in a plan, recursing into parallels and loops."""
    for node in nodes:
        if isinstance(node, GateNode):
            yield node
        elif isinstance(node, ParallelNode):
            yield from _iter_gates(node.steps)
        elif isinstance(node, LoopNode):
            yield from _iter_gates(node.body)


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #


def _cmd_validate(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    ws = _workspace(args)
    try:
        run_doc = load_run(run_dir)
    except (OSError, ValueError, ConfigError) as exc:
        print(f"cairn: cannot read {run_dir}/run.json: {exc}", file=sys.stderr)
        return int(ExitCode.CONFIG)

    pipeline = run_doc["pipeline"]
    params = {k: _param_str(v) for k, v in (run_doc.get("params") or {}).items() if v is not None}
    now = _parse_created_at(run_doc.get("created_at"))
    try:
        p = build_plan(ws, pipeline, params, now=now)
    except ConfigError as exc:
        return _print_config_error(exc)

    nodes = run_doc.get("nodes", {})
    only = args.artifact
    checked = 0
    failures = 0
    for step, _in_loop in _iter_steps(p.nodes):
        entry = nodes.get(step.id)
        if not entry or entry.get("status") != "done":
            continue
        cycle = entry.get("cycles")
        for name in step.produces:
            if only and name != only:
                continue
            decl = p.artifacts.get(name)
            if decl is None:
                continue
            use_cycle = cycle if "{cycle}" in decl.path else None
            rendered = render_artifact_path(decl, params=p.params, dims=p.dims, pipeline=p.pipeline, cycle=use_cycle, now=now)
            res = validate(resolve_path(decl, rendered, run_dir), decl, run_dir, ws)
            checked += 1
            if res.ok:
                print(f"{doctor_mod._OK} {name}  {rendered}")
            else:
                failures += 1
                print(f"{doctor_mod._BAD} {name}  {rendered}")
                for reason in res.reasons:
                    print(f"    - {reason}")
    if only and checked == 0:
        print(f"cairn: no done step produced artifact {only!r}", file=sys.stderr)
        return int(ExitCode.CONFIG)
    if checked == 0:
        print("cairn: no done-step artifacts to validate")
    return int(ExitCode.GATE_FAILED) if failures else int(ExitCode.OK)


def _parse_created_at(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return _now()


# --------------------------------------------------------------------------- #
# trail
# --------------------------------------------------------------------------- #


def _cmd_trail(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    if args.follow and args.json:
        return _trail_follow_json(run_dir, args.since)
    if args.watch:
        return _trail_watch(run_dir)
    # default: static pretty dump.
    for ev in read_trail(run_dir, since=args.since):
        print(_trail_line(ev))
    return int(ExitCode.OK)


def _trail_follow_json(run_dir: Path, since: int | None) -> int:
    try:
        for ev in follow(run_dir, since=since):
            sys.stdout.write(json.dumps(ev, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            if ev.get("event") in ("run-done", "run-halt"):
                break
    except KeyboardInterrupt:
        return int(ExitCode.OK)
    return int(ExitCode.OK)


_GLYPH = {"done": "✔", "running": "▶", "gate": "◆", "halted": "✗"}


def _trail_watch(run_dir: Path) -> int:
    """A basic re-rendered status tree derived from the trail (no curses)."""
    import time

    try:
        while True:
            statuses: dict[str, dict] = {}
            order: list[str] = []
            terminal = False
            for ev in read_trail(run_dir):
                node = ev.get("node")
                if node and node not in statuses:
                    order.append(node)
                if node:
                    statuses[node] = ev
                if ev.get("event") in ("run-done", "run-halt"):
                    terminal = True
            print("\n" + "─" * 40)
            for node in order:
                ev = statuses[node]
                event = ev.get("event", "?")
                glyph = "▶"
                if event in ("step-done", "gate-answered"):
                    glyph = "✔"
                elif event in ("run-halt", "step-fail"):
                    glyph = "✗"
                elif event == "gate-pending":
                    glyph = "◆"
                elif event == "step-skip":
                    glyph = "─"
                print(f"  {glyph} {node:16} {event}")
            if terminal or not sys.stdout.isatty():
                return int(ExitCode.OK)
            time.sleep(0.5)
    except KeyboardInterrupt:
        return int(ExitCode.OK)


def _trail_line(ev: dict) -> str:
    seq = ev.get("seq")
    event = ev.get("event", "?")
    node = ev.get("node") or "-"
    data = ev.get("data") or {}
    tail = json.dumps(data, sort_keys=True, ensure_ascii=False) if data else ""
    return f"{seq:>4}  {event:16} {node:16} {tail}"


# --------------------------------------------------------------------------- #
# ps
# --------------------------------------------------------------------------- #


def _cmd_ps(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    try:
        config = load_config(ws)
    except ConfigError as exc:
        return _print_config_error(exc)
    runs_root = _runs_root(ws, config)
    grace = config.defaults.heartbeat_s
    rows = []
    if runs_root.is_dir():
        for d in sorted(runs_root.iterdir()):
            if not (d / "run.json").is_file():
                continue
            try:
                doc = load_run(d)
            except (OSError, ValueError, ConfigError):
                continue
            st = derive_status(d, heartbeat_grace_s=grace)
            last = st.last_event or {}
            rows.append(
                {
                    "run": doc.get("run_id", d.name),
                    "status": st.status,
                    "node": st.node or "-",
                    "last_event": last.get("event", "-"),
                    "age": _age(last.get("at")),
                }
            )
    if args.json:
        print(json.dumps(rows, indent=2))
        return int(ExitCode.OK)
    if not rows:
        print("cairn: no runs found")
        return int(ExitCode.OK)
    print(f"{'RUN':30} {'STATUS':9} {'NODE':12} {'LAST EVENT':16} AGE")
    for r in rows:
        print(f"{r['run']:30} {r['status']:9} {r['node']:12} {r['last_event']:16} {r['age']}")
    return int(ExitCode.OK)


def _age(at: str | None) -> str:
    if not at:
        return "-"
    try:
        from datetime import timezone

        then = datetime.fromisoformat(at.replace("Z", "+00:00"))
        secs = (datetime.now(timezone.utc) - then).total_seconds()
    except (ValueError, TypeError):
        return "-"
    if secs < 90:
        return f"{int(secs)}s"
    if secs < 5400:
        return f"{int(secs // 60)}m"
    return f"{int(secs // 3600)}h"


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def _cmd_doctor(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    return doctor_mod.run_doctor(ws, executor=args.executor, probe_hooks=args.probe_hooks, now=_now())


# --------------------------------------------------------------------------- #
# test
# --------------------------------------------------------------------------- #

_SUITES = {"validators", "guards", "pipelines", "envelopes"}


def _cmd_test(args: argparse.Namespace) -> int:
    from cairn.kernel import testkit

    ws = _workspace(args)
    if args.suite == "record":
        if not args.rest:
            print("cairn: `cairn test record <run-dir>` needs a run dir", file=sys.stderr)
            return int(ExitCode.CONFIG)
        created = testkit.record_run(ws, Path(args.rest[0]), slim=args.slim)
        for path in created:
            print(path)
        print(f"cairn: recorded {len(created)} file(s)")
        return int(ExitCode.OK)

    suites = [args.suite] if args.suite else None
    try:
        report = testkit.run_all(ws, suites, update=args.update)
    except (ValueError, CairnError) as exc:
        print(f"cairn: {exc}", file=sys.stderr)
        return int(ExitCode.CONFIG)
    for name, res in report.suites.items():
        print(f"{name}: {res.passed} passed, {res.failed} failed")
        for note in res.notes:
            print(f"  note: {note}")
        for failure in res.failures:
            print(f"  FAIL: {failure}")
    return int(ExitCode.OK) if report.ok else 1


# --------------------------------------------------------------------------- #
# compose
# --------------------------------------------------------------------------- #


def _cmd_compose(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    now = _now()
    try:
        p = build_plan(ws, args.pipeline, _kv(args.param), now=now, headless=True)
        config = load_config(ws)
    except ConfigError as exc:
        return _print_config_error(exc)

    target = None
    in_loop = False
    for step, il in _iter_steps(p.nodes):
        if step.id == args.step:
            target, in_loop = step, il
            break
    if target is None:
        print(f"cairn: no step {args.step!r} in the planned range of {args.pipeline!r}", file=sys.stderr)
        return int(ExitCode.CONFIG)
    if target.kind != "agent":
        print(f"cairn: step {args.step!r} is a {target.kind} step — only agent steps have an envelope", file=sys.stderr)
        return int(ExitCode.CONFIG)

    composer = make_composer(workspace_dir=ws, config=config, now=now)
    if args.run_dir:
        run_dir = Path(args.run_dir)
        envelope = composer(target, p, run_dir, cycle=1 if in_loop else None, retry_reasons=[])
        sys.stdout.write(envelope)
    else:
        with tempfile.TemporaryDirectory() as td:
            envelope = composer(target, p, Path(td), cycle=1 if in_loop else None, retry_reasons=[])
            sys.stdout.write(envelope)
    return int(ExitCode.OK)


# --------------------------------------------------------------------------- #
# new
# --------------------------------------------------------------------------- #


def _cmd_new(args: argparse.Namespace) -> int:
    target = args.target
    name = args.name
    try:
        if target == "workspace":
            dest = newkit.new_workspace(name, Path(args.dir) if args.dir else None)
            print(f"cairn: workspace {name!r} created at {dest}")
            print(f"  cd {dest} && cairn run hello")
            return int(ExitCode.OK)
        if target in ("pipeline", "agent", "skill", "validator"):
            path = newkit.new_stub(target, name, _workspace(args))
            print(f"cairn: {target} {name!r} created at {path}")
            return int(ExitCode.OK)
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        print(f"cairn: {exc}", file=sys.stderr)
        return int(ExitCode.CONFIG)
    print(f"cairn: unknown new target {target!r}", file=sys.stderr)
    return int(ExitCode.CONFIG)


# --------------------------------------------------------------------------- #
# stubs (C6+)
# --------------------------------------------------------------------------- #


def _cmd_stub(args: argparse.Namespace) -> int:
    print(f"cairn: {args.command} is not implemented — see IMPLEMENTATION-PLAN C6+", file=sys.stderr)
    return int(ExitCode.CONFIG)


# --------------------------------------------------------------------------- #
# Parser.
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cairn", description="cairn pipeline orchestrator")
    parser.add_argument("--version", action="version", version=f"cairn {cairn.__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def param_flag(sp):
        sp.add_argument("--param", action="append", metavar="k=v", help="pipeline param (repeatable)")

    # plan
    sp = sub.add_parser("plan", help="typecheck a pipeline and print its execution plan")
    sp.add_argument("pipeline")
    param_flag(sp)
    sp.add_argument("--executor")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--to")
    sp.add_argument("--from", dest="from_node")
    sp.set_defaults(func=_cmd_plan)

    # run
    sp = sub.add_parser("run", help="plan then execute a pipeline")
    sp.add_argument("pipeline")
    param_flag(sp)
    sp.add_argument("--executor")
    sp.add_argument("--step-executor", action="append", metavar="STEP=X", help="per-step executor (repeatable)")
    sp.add_argument("--gate", action="append", metavar="NAME=CHOICE", help="preset a gate choice (repeatable)")
    sp.add_argument("--headless", action="store_true")
    sp.add_argument("--to")
    sp.add_argument("--from", dest="from_node")
    sp.add_argument("--run-dir")
    sp.add_argument("--idempotent", action="store_true")
    sp.set_defaults(func=_cmd_run)

    # resume
    sp = sub.add_parser("resume", help="resume a run dir")
    sp.add_argument("run_dir", metavar="run-dir")
    sp.add_argument("--force", action="store_true", help="accept pipeline-hash drift")
    sp.set_defaults(func=_cmd_resume)

    # gate
    sp = sub.add_parser("gate", help="answer a pending gate externally")
    sp.add_argument("run_dir", metavar="run-dir")
    sp.add_argument("assignment", metavar="name=choice")
    sp.set_defaults(func=_cmd_gate)

    # validate
    sp = sub.add_parser("validate", help="re-validate a run's produced artifacts")
    sp.add_argument("run_dir", metavar="run-dir")
    sp.add_argument("artifact", nargs="?")
    sp.set_defaults(func=_cmd_validate)

    # trail
    sp = sub.add_parser("trail", help="read a run's trail")
    sp.add_argument("run_dir", metavar="run-dir")
    sp.add_argument("--watch", action="store_true")
    sp.add_argument("--follow", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--since", type=int)
    sp.set_defaults(func=_cmd_trail)

    # ps
    sp = sub.add_parser("ps", help="cross-run fleet view")
    sp.add_argument("--workspace", default=".")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_ps)

    # doctor
    sp = sub.add_parser("doctor", help="machine + workspace preflight")
    sp.add_argument("--executor")
    sp.add_argument("--probe-hooks", action="store_true")
    sp.set_defaults(func=_cmd_doctor)

    # test
    sp = sub.add_parser("test", help="the offline L1 suites (or `test record <run-dir>`)")
    sp.add_argument("suite", nargs="?", choices=sorted(_SUITES | {"record"}))
    sp.add_argument("rest", nargs="*")
    sp.add_argument("--update", action="store_true")
    sp.add_argument("--slim", action="store_true")
    sp.set_defaults(func=_cmd_test)

    # new
    sp = sub.add_parser("new", help="scaffold a workspace or a single-file stub")
    sp.add_argument("target", choices=["workspace", "pipeline", "agent", "skill", "validator"])
    sp.add_argument("name")
    sp.add_argument("--dir", help="parent dir for `new workspace`")
    sp.set_defaults(func=_cmd_new)

    # compose
    sp = sub.add_parser("compose", help="render a step's envelope without executing")
    sp.add_argument("pipeline")
    sp.add_argument("step")
    param_flag(sp)
    sp.add_argument("--run-dir")
    sp.set_defaults(func=_cmd_compose)

    # stubs (C6+)
    for name in ("batch", "learnings", "gc", "schedule"):
        sp = sub.add_parser(name, help=f"{name} (not implemented — C6+)")
        sp.set_defaults(func=_cmd_stub)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return int(ExitCode.CONFIG)
    try:
        return args.func(args)
    except _Usage as exc:
        print(f"cairn: {exc}", file=sys.stderr)
        return int(ExitCode.CONFIG)
    except BrokenPipeError:  # `cairn trail … | head` — a closed downstream pipe is not an error
        return int(ExitCode.OK)
