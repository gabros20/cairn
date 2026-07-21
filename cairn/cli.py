"""cairn CLI — the argparse frame plus the thin wiring that turns the kernel into a tool.

Every subcommand parses its flags, wires kernel objects together, and prints; the logic
stays in the kernel. Verbs (docs/API.md §9): plan/run/resume/gate/validate/trail/ps/doctor/
test/compose/new plus the fleet verbs batch/learnings/gc/schedule/trigger are all live —
each is a thin binding over its kernel module (batchkit/learnkit/gckit/schedkit/triggerkit).

Guard wiring (the pinned contract): in run/resume, a plan's ``shim``-enforced guards get a
fresh PATH-shim dir per run (:func:`~cairn.kernel.guards.build_shims`), and every executor is
wrapped in a :class:`GuardedExecutor` that PREPENDS the shim dir to each invocation's PATH.
Hook-layer wiring: the walker calls each executor's ``install_guards`` once before the node
loop; ``ClaudeExecutor`` installs a ``PreToolUse`` hook (``codex``/``grok`` install is still a
no-op).
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import cairn
from cairn.executors._cli import _probe_version
from cairn.kernel import doctor as doctor_mod
from cairn.kernel import newkit
from cairn.kernel.artifacts import resolve_path, validate
from cairn.kernel.batchkit import run_batch
from cairn.kernel.compose import make_composer, render_artifact_path
from cairn.kernel.config import Config, ExecutorConfig, installed_version, load_config, version_compat
from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.gatekit import (
    GateTampered,
    GateUnanswered,
    answer_gate,
    is_answered,
    read_verified_choice,
)
from cairn.kernel.gckit import apply_gc, plan_gc
from cairn.kernel.guards import build_shims
from cairn.kernel.learnkit import collect_learnings, render_learnings
from cairn.kernel.plan import (
    GateNode,
    LoopNode,
    ParallelNode,
    Plan,
    StepNode,
    plan as build_plan,
)
from cairn.kernel.proc import SubprocessRunner as _SubprocessRunner
from cairn.kernel.runstate import load_run, update_run
from cairn.kernel.toolcheck import run_tool_check
from cairn.kernel.schedkit import (
    diff_schedules,
    find_idempotent_run,
    install as install_schedules,
    list_installed,
    load_schedules,
    run_schedule,
    uninstall as uninstall_schedules,
)
from cairn.kernel.trail import derive_status, follow, read_trail
from cairn.kernel.triggerkit import (
    TriggerStatus,
    list_installed_triggers,
    remove_trigger,
    run_trigger,
    sync_triggers,
)
from cairn.kernel.types import ExitCode
from cairn.kernel.walk import bootstrap_run, invalidate_from, walk

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
    "trigger",
]

# --------------------------------------------------------------------------- #
# Small shared helpers.
# --------------------------------------------------------------------------- #


def _now() -> datetime:
    # Aware UTC — the one clock source behind run.json `created_at`, node `at` (both stamped
    # Z-terminated by trail.format_at), plan/{date} templating, doctor, and gatekit. A naive
    # local clock here would be *labeled* UTC downstream — wrong by the local offset.
    return datetime.now(timezone.utc)


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
    shim-dir/manifest env the shims read. The native hook layer is installed separately by the
    walker via ``install_guards`` (``claude`` writes a ``PreToolUse`` hook), not by this wrapper.
    """

    def __init__(self, inner: Any, delta: dict[str, str], shim_dir: Path) -> None:
        self._inner = inner
        self._delta = delta
        self._shim_dir = Path(shim_dir)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def invoke(self, inv):
        # CAIRN_SHIM_MANIFEST points at the SIGNED manifest OUTSIDE the run dir. Env-first (C9):
        # if the walker already set a per-invocation manifest in inv.env (a runtime-`when` guard
        # is in play), HONOR it — it must win over the static delta or the walker's `when`
        # decision would be clobbered right back to "always enforce". No runtime-`when` guards →
        # inv.env carries no override → falls back to the static delta, unchanged from before C9.
        env = {
            **inv.env,
            "PATH": f"{self._delta['PATH']}:{inv.env.get('PATH', '')}",
            "CAIRN_SHIM_DIR": self._delta["CAIRN_SHIM_DIR"],
            "CAIRN_SHIM_MANIFEST": inv.env.get("CAIRN_SHIM_MANIFEST", self._delta["CAIRN_SHIM_MANIFEST"]),
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
    delta = build_shims(
        shim_guards, shim_dir=shim_dir, workspace_dir=Path(workspace_dir), run_dir=Path(run_dir)
    )
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


def _preflight_tools(p: Plan) -> int | None:
    """The `cairn run` hard-stop (docs/TOOLING-AND-GROWTH.md §2): run each in-range scoped tool's
    ``check`` BEFORE anything is minted or walked. Wired into every entrance about to execute —
    a fresh mint (before ``bootstrap_run``, so a failure creates nothing on disk), both resume
    entrances of `cairn run`, and `cairn resume` — always AFTER the resume guards (drift →
    version → tools, the same order on every path). Only the `--idempotent` entrances
    short-circuit an already-complete run before any check; the non-idempotent resume entrances
    re-check even when run.json says "done" — deliberate, not an oversight: the walk's
    skip-if-valid semantics re-execute a "done" step whose artifact has since been deleted or
    invalidated (see ``walk._is_done``), and that repair re-execution still needs its tools, so
    a status-done fast-path here would be wrong, not just redundant.

    A failing check refuses with a legible message naming the tool, the step(s)/pipeline needing
    it, the failed check, and the fix (install hint + `cairn doctor`), and returns
    ``ExitCode.CONFIG``. All checks pass → returns None with zero output. ``p.tool_requirements``
    already excludes unscoped and out-of-range tools, so this runs no subprocess a plan-time
    scope didn't already justify."""
    failures = [req for req in p.tool_requirements if not run_tool_check(req.check)]
    if not failures:
        return None
    lines = [f"cairn: refusing to run {p.pipeline!r} — required tool(s) unverified on this machine:"]
    for req in failures:
        lines.append(f"  ✗ {req.tool}  `{req.check}` failed (needed by: {', '.join(req.targets)})")
        if req.install:
            lines.append(f"      fix: {req.install}")
    lines.append("  → run `cairn doctor` to verify tooling, then re-run.")
    print("\n".join(lines), file=sys.stderr)
    return int(ExitCode.CONFIG)


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
        if not existing:
            # Fresh mint: verify in-range tools BEFORE minting — a failing check refuses with
            # nothing created on disk (docs/TOOLING-AND-GROWTH §2).
            fail = _preflight_tools(p)
            if fail is not None:
                return fail
            run_dir = bootstrap_run(ws, p, now=now, run_dir=run_dir, pipeline_hash=phash)
            created = True
        else:
            # An existing --run-dir resumes (with or without --idempotent) — through the same
            # sequence as every other resume entrance: guards (drift → version), then the tool
            # hard-stop. `cairn run` has no --force; the refusals name
            # `cairn resume <run-dir> --force` as the escape hatch.
            fail = _resume_guards(run_dir, phash, p.pipeline, ws, force=False)
            if fail is not None:
                return fail
            fail = _preflight_tools(p)
            if fail is not None:
                return fail
    else:
        runs_root = _runs_root(ws, config)
        # Idempotency's single source of truth is schedkit.find_idempotent_run — it matches by
        # the (pipeline, params, {date}) content key a scheduled `--idempotent` firing would use,
        # not by run-id string. Complete match → no-op; incomplete → resume; none → fresh run.
        match = (
            find_idempotent_run(runs_root, pipeline=p.pipeline, params=p.params, now=now)
            if args.idempotent
            else None
        )
        if match is not None and match.complete:
            print(f"cairn: already done → {match.run_dir}")
            return int(ExitCode.OK)
        if match is not None:
            # Incomplete equivalent run → resume it, never mint a variant — but through the
            # same sequence `cairn resume` enforces: guards (a timer re-fire after a pipeline
            # edit or a cross-major cairn upgrade must fail loud, not silently resume), then
            # the tool hard-stop. `cairn run` has no --force flag; only
            # `cairn resume … --force` can override.
            fail = _resume_guards(match.run_dir, phash, p.pipeline, ws, force=False)
            if fail is not None:
                return fail
            fail = _preflight_tools(p)
            if fail is not None:
                return fail
            run_dir = match.run_dir
        else:
            # Fresh mint: verify in-range tools BEFORE minting (nothing on disk on failure).
            fail = _preflight_tools(p)
            if fail is not None:
                return fail
            run_dir = bootstrap_run(ws, p, now=now, runs_root=runs_root, pipeline_hash=phash)
            created = True

    if created:
        # Record the actual executor routing so `cairn resume` reconstructs the same fleet
        # (mixed --executor / --step-executor) instead of silently falling back to defaults.
        global_default = (None if stub_mode else args.executor) or config.workspace.default_executor or ""
        _record_executor_routing(run_dir, now, global_default, _kv(args.step_executor), p.resolved_models, ws)

    interactive = (not args.headless) and sys.stdin.isatty()
    try:
        return _drive(
            p, run_dir, ws, config,
            interactive=interactive, gate_presets=_kv(args.gate), now=now, stub_mode=stub_mode,
        )
    except CairnError as exc:
        print(f"cairn: {exc}", file=sys.stderr)
        return int(ExitCode.EXECUTOR)


def _probe_executor_versions(resolved_models: dict[str, tuple[str, str, str | None]]) -> dict[str, str | None]:
    """``<cli> --version`` for each DISTINCT executor the plan actually resolves (ARCHITECTURE
    §10) — never ``shell``/``stub``, which aren't CLI-backed and don't understand the flag.

    A probe failure (binary not on PATH, non-zero exit, timeout, or a spawn failure) records
    ``None`` for that executor and moves on — a version probe is best-effort telemetry, never
    a reason to crash the mint. ``shutil.which`` only rules out the common case; it's a TOCTOU
    check (the binary can vanish between the check and the spawn) and doesn't catch a
    resolvable-but-broken shim (asdf/mise/nvm-managed CLIs are a realistic case on dev
    machines), so the probe itself is still wrapped: ``run_process`` (via ``_probe_version``)
    raises :class:`~cairn.kernel.errors.ExecutorSpawnError` — a :class:`CairnError`, NOT an
    ``OSError`` — on a Popen spawn failure, not just a bare ``OSError``."""
    names = sorted({exec_name for exec_name, _model, _effort in resolved_models.values()})
    out: dict[str, str | None] = {}
    for name in names:
        if shutil.which(name) is None:
            out[name] = None
            continue
        try:
            code, text = _probe_version(name)
        except (OSError, CairnError):
            out[name] = None
            continue
        out[name] = text if (code == 0 and text) else None
    return out


def _workspace_git_rev(ws: Path) -> dict[str, Any] | None:
    """The workspace's git ``HEAD`` + dirty flag for run.json's reproducibility record
    (ARCHITECTURE §10), or ``None`` when ``ws`` isn't inside a git repo (or ``git`` itself is
    unavailable). Never raises — a missing binary/non-repo workspace is absent, not a mint
    failure, mirroring ``_probe_executor_versions``'s probe-failure handling."""
    runner = _SubprocessRunner()
    try:
        head = runner.run(["git", "-C", str(ws), "rev-parse", "HEAD"])
    except OSError:
        return None
    if head.returncode != 0 or not head.stdout.strip():
        return None
    try:
        status = runner.run(["git", "-C", str(ws), "status", "--porcelain"])
    except OSError:
        status = None
    dirty = bool(status.stdout.strip()) if status is not None and status.returncode == 0 else False
    return {"rev": head.stdout.strip(), "dirty": dirty}


def _record_executor_routing(
    run_dir: Path,
    now: datetime,
    default: str,
    overrides: dict[str, str],
    resolved_models: dict[str, tuple[str, str, str | None]],
    ws: Path,
) -> None:
    """Persist the run's executor fleet into run.json (schema §8.1 ``executors``) so resume is
    faithful, AND the reproducibility record ARCHITECTURE §10 promises: each resolved
    executor's probed ``--version`` and the workspace's git rev + dirty flag at mint (``None``
    for either on probe failure / a non-git workspace — never a mint crash)."""
    versions = _probe_executor_versions(resolved_models)
    git = _workspace_git_rev(ws)

    def mutate(doc: dict) -> None:
        ex = doc.setdefault("executors", {})
        ex["default"] = default
        ex["overrides"] = dict(overrides)
        ex["versions"] = versions
        doc["git_rev"] = git["rev"] if git is not None else None
        doc["git_dirty"] = git["dirty"] if git is not None else None

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


def _pipeline_drift_guard(recorded: str | None, current_hash: str, pipeline: str, run_dir: Path, *, force: bool) -> int | None:
    """The pipeline-hash drift check shared by ``cairn resume`` and ``run --idempotent``'s
    resume path: a recorded hash that matches neither the current file nor the pre-hash
    sentinel means the pipeline changed under the run. Returns the exit code to fail with,
    or None to proceed (with a warning when ``force`` overrode a real drift). The remedy
    names the command that actually takes ``--force`` (``cairn run`` has no such flag)."""
    if not recorded or recorded in ("sha256:unknown", current_hash):
        return None
    if not force:
        print(
            f"cairn: pipeline {pipeline!r} has changed since this run was planned "
            f"(hash drift). Run `cairn resume {run_dir} --force` to resume against "
            f"the current file.",
            file=sys.stderr,
        )
        return int(ExitCode.CONFIG)
    print(f"cairn: warning — resuming across pipeline-hash drift (--force) for {pipeline!r}", file=sys.stderr)
    return None


def _version_compat_guard(recorded: str | None, run_dir: Path, *, force: bool) -> int | None:
    """The cross-version resume gate shared by ``cairn resume`` and ``run --idempotent``'s
    resume path (docs/DISTRIBUTION.md §3, *Run-dir format*): compare the cairn version that
    created this run against the installed one. Same major.minor resumes silently; a
    cross-minor drift or an unrecorded/legacy version warns and proceeds; a cross-major
    difference refuses without ``--force`` (run-dir semantics may not carry across a major).
    Returns the exit code to fail with, or None to proceed. Mirrors `_pipeline_drift_guard`,
    including the remedy naming the command that actually takes ``--force``."""
    installed = installed_version()
    verdict = version_compat(recorded, installed)
    if verdict == "ok":
        return None
    if verdict == "warn":
        if recorded:
            print(
                f"cairn: warning — resuming a run created by cairn {recorded} on cairn "
                f"{installed} (version drift)",
                file=sys.stderr,
            )
        else:
            print(
                f"cairn: warning — this run dir records no cairn version; resuming on cairn "
                f"{installed}",
                file=sys.stderr,
            )
        return None
    # verdict == "refuse" — cross-major.
    if not force:
        print(
            f"cairn: this run was created by cairn {recorded} but cairn {installed} is "
            f"installed (major-version drift). Run `cairn resume {run_dir} --force` to "
            f"resume against the installed version.",
            file=sys.stderr,
        )
        return int(ExitCode.CONFIG)
    print(
        f"cairn: warning — resuming across cairn-version drift (--force): "
        f"run {recorded} vs installed {installed}",
        file=sys.stderr,
    )
    return None


def _repin_manifest(run_dir: Path, run_doc: dict, current_hash: str) -> None:
    """After an explicit ``--force`` accepted drift, re-pin run.json to the present:
    ``cairn_version`` becomes the installed one and ``pipeline_hash`` the current file's.
    Without this every later resume of the run trips the same guard and pays ``--force``
    forever — the consent was already given once (issue #2). No-op when nothing drifted;
    ``run_doc`` is updated in place so the rest of the resume sees the re-pinned values."""
    updates: dict[str, str] = {}
    installed = installed_version()
    if run_doc.get("cairn_version") != installed:
        updates["cairn_version"] = installed
    if run_doc.get("pipeline_hash") != current_hash:
        updates["pipeline_hash"] = current_hash
    if not updates:
        return
    update_run(run_dir, lambda doc: doc.update(updates))
    run_doc.update(updates)
    print(
        f"cairn: --force — re-pinned {', '.join(sorted(updates))} in run.json "
        f"(later resumes won't need --force)",
        file=sys.stderr,
    )


def _reproducibility_drift_guard(recorded: dict, ws: Path) -> None:
    """Warn — never refuse — when an executor version or the workspace git rev recorded at
    mint (ARCHITECTURE §10) has drifted by resume time. Unlike ``_pipeline_drift_guard``
    (the pipeline itself changed under the run — potentially unsafe to resume against) or
    ``_version_compat_guard``'s cross-major case (run-dir semantics may not carry across a
    cairn major), a newer CLI or a workspace commit since mint doesn't invalidate what's on
    disk — so this never blocks and takes no ``--force``. A probe failure here is silent (not
    a second, redundant warning): the tool hard-stop / doctor already own "can this run at
    all"; this guard only speaks up when a probe SUCCEEDS with a value that disagrees. Catches
    both ``OSError`` and :class:`CairnError` — a Popen spawn failure surfaces as
    ``ExecutorSpawnError`` (a ``CairnError``), not a bare ``OSError`` (see
    ``_probe_executor_versions``). NOTE: only ``git_rev`` is drift-compared here — ``git_dirty``
    is recorded at mint but a clean↔dirty change alone (same rev) is never itself warned on."""
    recorded_versions = (recorded.get("executors") or {}).get("versions") or {}
    for name, old in sorted(recorded_versions.items()):
        if not old or shutil.which(name) is None:
            continue
        try:
            code, current = _probe_version(name)
        except (OSError, CairnError):
            continue
        if code == 0 and current and current != old:
            print(
                f"cairn: warning — executor {name!r} reports {current!r} at resume, "
                f"recorded {old!r} at mint (version drift)",
                file=sys.stderr,
            )

    recorded_git = recorded.get("git_rev")
    if recorded_git:
        current = _workspace_git_rev(ws)
        if current is not None and current["rev"] != recorded_git:
            print(
                f"cairn: warning — workspace is at git {current['rev']} at resume, "
                f"recorded {recorded_git} at mint (workspace drift)",
                file=sys.stderr,
            )


def _resume_guards(run_dir: Path, phash: str, pipeline: str, ws: Path, *, force: bool) -> int | None:
    """The shared gate for ``cairn run``'s two resume entrances (``--run-dir <existing>``
    and ``--idempotent``'s auto-match): read the manifest fail-loud, then drift → version →
    reproducibility (advisory) — the same order ``cairn resume`` applies them. A dir chosen
    for resume whose run.json can't be read (or fails the cairn:run schema) is a config
    error: degrading to ``{}`` would print a misleading "records no cairn version" warning
    and then crash later inside the walk. Returns the exit code to fail with, or None to
    proceed. (``_cmd_resume`` stays separate — its force comes from argv and it needs the run
    doc for params/executors.)"""
    try:
        recorded = load_run(run_dir)
    except (OSError, ValueError, ConfigError) as exc:
        print(f"cairn: cannot read {run_dir}/run.json: {exc}", file=sys.stderr)
        return int(ExitCode.CONFIG)
    fail = _pipeline_drift_guard(recorded.get("pipeline_hash"), phash, pipeline, run_dir, force=force)
    if fail is not None:
        return fail
    fail = _version_compat_guard(recorded.get("cairn_version"), run_dir, force=force)
    if fail is not None:
        return fail
    _reproducibility_drift_guard(recorded, ws)
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

    phash = _pipeline_hash(ws, pipeline)
    fail = _pipeline_drift_guard(run_doc.get("pipeline_hash"), phash, pipeline, run_dir, force=args.force)
    if fail is not None:
        return fail
    fail = _version_compat_guard(run_doc.get("cairn_version"), run_dir, force=args.force)
    if fail is not None:
        return fail
    _reproducibility_drift_guard(run_doc, ws)
    if args.force:
        _repin_manifest(run_dir, run_doc, phash)

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

    # Resume re-checks in-range tools: a tool can vanish between sessions and the steps ahead
    # still need it (docs/TOOLING-AND-GROWTH §2). Cheap, and nothing is walked on a failure.
    fail = _preflight_tools(p)
    if fail is not None:
        return fail

    if args.from_node:
        try:
            cleared, moved = invalidate_from(p, run_dir, args.from_node, now=now)
        except ConfigError as exc:
            return _print_config_error(exc)
        except CairnError as exc:  # LockHeldError — another process holds the run
            print(f"cairn: {exc}", file=sys.stderr)
            return int(ExitCode.EXECUTOR)
        print(
            f"cairn: --from {args.from_node!r} — cleared {cleared} node record(s), "
            f"superseded {moved} artifact file(s)",
            file=sys.stderr,
        )

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
        # Quote the recorded choice only if it AUTHENTICATES — never echo an unverified
        # attacker-controlled value back to the operator as if it were the real decision.
        try:
            recorded = repr(read_verified_choice(run_dir, gate))
        except GateTampered:
            recorded = "unverifiable — decision file failed authentication"
        except GateUnanswered:
            recorded = "unreadable"
        print(f"cairn: gate {name!r} is already answered ({recorded}); refusing to overwrite", file=sys.stderr)
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
# batch — a pool of `cairn run --headless` children over a JSONL params file.
# --------------------------------------------------------------------------- #


def _cmd_batch(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    gate_presets = _kv(args.gate)  # _Usage → exit 2 via main()
    # --to/--from are pass-through: appended to every child `cairn run` argv verbatim, so each
    # child validates the range exactly as an interactive run (an unknown node fails it CONFIG).
    extra_args: list[str] = []
    if args.to:
        extra_args += ["--to", args.to]
    if args.from_node:
        extra_args += ["--from", args.from_node]
    try:
        result = run_batch(
            ws,
            args.pipeline,
            Path(args.params_file),
            jobs=args.jobs,
            gate_presets=gate_presets,
            extra_args=extra_args,
            out=sys.stdout,
        )
    except ConfigError as exc:
        return _print_config_error(exc)

    print(f"cairn: batch {result.pipeline} — {result.total} run(s), {len(result.failed)} failed")
    # A few stderr-tail lines per failed run, indented under its line — enough to name the
    # failure (gate/config/executor error) without flooding; the rest lives in the run dir.
    _SUMMARY_ERROR_LINES = 6
    for o in result.failed:
        rd = o.run_dir.name if o.run_dir is not None else "?"
        print(f"  ✗ [{o.index}] {rd}  exit {o.exit_code}", file=sys.stderr)
        if not o.error:
            continue
        tail_lines = o.error.splitlines()
        for line in tail_lines[:_SUMMARY_ERROR_LINES]:
            print(f"      {line}", file=sys.stderr)
        if len(tail_lines) > _SUMMARY_ERROR_LINES:
            extra = len(tail_lines) - _SUMMARY_ERROR_LINES
            where = f"; see {o.run_dir}" if o.run_dir is not None else ""
            print(f"      … (+{extra} more line(s){where})", file=sys.stderr)
    return int(result.exit_code)


# --------------------------------------------------------------------------- #
# learnings — aggregate `learn` trail events across every run.
# --------------------------------------------------------------------------- #


def _cmd_learnings(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    try:
        config = load_config(ws)
    except ConfigError as exc:
        return _print_config_error(exc)
    runs_root = _runs_root(ws, config)

    warnings: list[str] = []
    try:
        learnings = collect_learnings(runs_root, since=args.since, tag=args.tag, warnings=warnings)
    except ValueError as exc:
        print(f"cairn: invalid --since {args.since!r}: {exc}", file=sys.stderr)
        return int(ExitCode.CONFIG)
    for w in warnings:
        print(f"cairn: {w}", file=sys.stderr)
    print(render_learnings(learnings))
    return int(ExitCode.OK)


# --------------------------------------------------------------------------- #
# gc — explicit retention over the runs root (dry-run by default; --apply deletes).
# --------------------------------------------------------------------------- #


def _cmd_gc(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    try:
        config = load_config(ws)
    except ConfigError as exc:
        return _print_config_error(exc)
    runs_root = _runs_root(ws, config)

    if args.keep_days is None and args.keep_last is None:
        print(
            "cairn: gc needs a retention rule — pass at least one of --keep-days N or --keep-last M",
            file=sys.stderr,
        )
        return int(ExitCode.CONFIG)

    plan = plan_gc(
        runs_root,
        keep_days=args.keep_days,
        keep_last=args.keep_last,
        artifacts_only=args.artifacts_only,
        now=datetime.now(timezone.utc),  # gckit requires an aware UTC clock
        include_needs_human=args.include_needs_human,
    )

    if not args.apply:
        _print_gc_plan(plan)
        return int(ExitCode.OK)

    result = apply_gc(plan)
    _print_gc_result(result, plan)
    return int(ExitCode.OK)


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{n}B"


def _print_gc_plan(plan) -> None:
    verb = "slim" if plan.artifacts_only else "delete"
    if plan.candidates:
        print(f"cairn: gc dry-run — {len(plan.candidates)} run(s) would {verb} (pass --apply to execute):")
        for c in plan.candidates:
            age = f"{c.age_days:.1f}d" if c.age_days is not None else "?"
            print(f"  ⌫ {c.run_id}  ({c.reason}, age {age})")
    else:
        print("cairn: gc dry-run — no runs selected (pass --apply once a rule matches something)")
    for run_id, reason in plan.skipped:
        print(f"  ─ {run_id}  skipped: {reason}")


def _print_gc_result(result, plan) -> None:
    verb = "slimmed" if plan.artifacts_only else "deleted"
    print(f"cairn: gc {verb} {len(result.deleted)} run(s), freed {_human_bytes(result.freed_bytes)}")
    for run_id in result.deleted:
        print(f"  ⌫ {run_id}")
    for run_id, reason in result.errors:
        print(f"  ! {run_id}: {reason}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# schedule — sync schedules.yaml into the host scheduler (install/list/run/uninstall).
# --------------------------------------------------------------------------- #


# The schedkit ``Runner`` adapter is ``_SubprocessRunner`` — imported at the top of this file
# as an alias of ``cairn.kernel.proc.SubprocessRunner`` (the shared subprocess seam).


def _resolve_cairn_bin() -> str:
    """The command host timer entries invoke. schedkit/triggerkit render it as a single
    shlex-quoted token (no multi-token ``python -m cairn`` form), so resolve the console
    script honestly: its absolute path when on PATH; else the sibling console script next
    to the *running* interpreter (every venv/uv install places one there, so this resolves
    even when the venv's bin/ isn't on PATH — e.g. a bare `pytest` in an activated venv);
    else the bare name with a doctor-style warning."""
    found = shutil.which("cairn")
    if found:
        return found
    sibling = Path(sys.executable).parent / "cairn"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    print(
        "cairn: warning — 'cairn' is not on PATH; installed timer entries will invoke the bare "
        "name 'cairn' and may fail to launch. Install the console script (pip/uv) or add it to PATH.",
        file=sys.stderr,
    )
    return "cairn"


def _schedule_dirs(args: argparse.Namespace) -> tuple[Path, Path]:
    """The per-user default target dirs for the host-file backends (overridable via flags)."""
    launchd = Path(args.launchd_dir).expanduser() if args.launchd_dir else Path.home() / "Library" / "LaunchAgents"
    systemd = Path(args.systemd_dir).expanduser() if args.systemd_dir else Path.home() / ".config" / "systemd" / "user"
    return launchd, systemd


def _cmd_schedule(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    backend = args.backend
    runner = _SubprocessRunner()
    launchd_dir, systemd_dir = _schedule_dirs(args)

    try:
        if args.action == "run":
            if not args.name:
                print("cairn: `cairn schedule run` needs a schedule name", file=sys.stderr)
                return int(ExitCode.CONFIG)
            schedules = load_schedules(ws)
            # Propagate the child's exit code verbatim (SCHEDULING.md §2), and thread our
            # real streams through so the Runner-captured child output — halt reasons, the
            # resume hint — is re-emitted: a cron-fired halt must mail, never rot silently.
            return run_schedule(
                schedules,
                args.name,
                workspace_dir=ws,
                runner=runner,
                cairn_bin=_resolve_cairn_bin(),
                out=sys.stdout,
                err=sys.stderr,
            )

        if args.action == "install":
            schedules = load_schedules(ws)
            install_schedules(
                schedules,
                backend,
                workspace_dir=ws,
                runner=runner,
                cairn_bin=_resolve_cairn_bin(),
                launchd_dir=launchd_dir,
                systemd_dir=systemd_dir,
            )
            print(f"cairn: installed {len(schedules)} schedule(s) into the {backend} backend")
            return int(ExitCode.OK)

        if args.action == "uninstall":
            uninstall_schedules(
                backend,
                workspace_dir=ws,
                runner=runner,
                launchd_dir=launchd_dir,
                systemd_dir=systemd_dir,
            )
            print(f"cairn: removed cairn-managed schedules from the {backend} backend")
            return int(ExitCode.OK)

        if args.action == "list":
            schedules = load_schedules(ws)
            installed = list_installed(
                backend, runner=runner, launchd_dir=launchd_dir, systemd_dir=systemd_dir
            )
            _print_schedule_diff(diff_schedules(schedules, installed), backend)
            return int(ExitCode.OK)
    except ConfigError as exc:
        return _print_config_error(exc)
    except FileNotFoundError as exc:
        # The Runner shelled out to a binary the host can't find (a bare `cairn` off PATH,
        # or a missing crontab/launchctl/systemctl) — a clean exit 2, never a traceback.
        missing = exc.filename or "cairn"
        print(
            f"cairn: cannot execute {missing!r} — the binary is not on PATH. Install the cairn "
            "console script (pip/uv tool install cairn) or put it on the PATH the scheduler uses.",
            file=sys.stderr,
        )
        return int(ExitCode.CONFIG)

    print(f"cairn: unknown schedule action {args.action!r}", file=sys.stderr)
    return int(ExitCode.CONFIG)


def _print_schedule_diff(diff, backend: str) -> None:
    print(f"cairn: schedules (declared vs installed on {backend}):")
    rows = (
        [(n, "+ declared, not installed") for n in diff.added]
        + [(n, "~ changed (re-run install)") for n in diff.changed]
        + [(n, "- installed, not declared") for n in diff.removed]
        + [(n, "= in sync") for n in diff.unchanged]
    )
    if not rows:
        print("  (none)")
    for name, note in sorted(rows):
        print(f"  {note:32} {name}")


# --------------------------------------------------------------------------- #
# trigger — sync triggers.yaml into the host watcher (sync/list/remove/run, TRIGGERS.md).
# --------------------------------------------------------------------------- #


def _cmd_trigger(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    backend = args.backend
    runner = _SubprocessRunner()
    # Same target-dir resolution schedule uses (`--launchd-dir`/`--systemd-dir`, same
    # per-user defaults) — triggerkit's sync/list/remove take the identical two knobs.
    launchd_dir, systemd_dir = _schedule_dirs(args)

    try:
        if args.action == "run":
            if not args.name:
                print("cairn: `cairn trigger run` needs a trigger name", file=sys.stderr)
                return int(ExitCode.CONFIG)
            # The exit code IS the contract (requirement 3): claim hazards and a failed
            # child both surface here, verbatim — never remapped into an ExitCode member.
            return run_trigger(
                args.name,
                ws,
                runner=runner,
                cairn_bin=_resolve_cairn_bin(),
                now=_now(),
            )

        if args.action == "sync":
            names = sync_triggers(
                ws,
                backend=backend,
                runner=runner,
                cairn_bin=_resolve_cairn_bin(),
                launchd_dir=launchd_dir,
                systemd_dir=systemd_dir,
            )
            print(f"cairn: synced {len(names)} trigger unit(s) into the {backend} backend")
            return int(ExitCode.OK)

        if args.action == "remove":
            if not args.name:
                print("cairn: `cairn trigger remove` needs a trigger name", file=sys.stderr)
                return int(ExitCode.CONFIG)
            removed = remove_trigger(
                args.name,
                ws,
                backend=backend,
                runner=runner,
                launchd_dir=launchd_dir,
                systemd_dir=systemd_dir,
            )
            if removed:
                print(f"cairn: removed trigger {args.name!r} from the {backend} backend")
            else:
                print(f"cairn: no trigger named {args.name!r} installed on the {backend} backend")
            return int(ExitCode.OK)

        if args.action == "list":
            statuses = list_installed_triggers(
                ws, backend=backend, runner=runner, launchd_dir=launchd_dir, systemd_dir=systemd_dir
            )
            if args.json:
                print(json.dumps([_trigger_status_json(s) for s in statuses]))
            else:
                _print_trigger_list(statuses, backend)
            return int(ExitCode.OK)
    except ConfigError as exc:
        return _print_config_error(exc)
    except FileNotFoundError as exc:
        # Same remedy as schedule's identical guard: a Runner call shelled out to a
        # binary the host can't find (bare `cairn`/launchctl/systemctl off PATH).
        missing = exc.filename or "cairn"
        print(
            f"cairn: cannot execute {missing!r} — the binary is not on PATH. Install the cairn "
            "console script (pip/uv tool install cairn) or put it on the PATH the scheduler uses.",
            file=sys.stderr,
        )
        return int(ExitCode.CONFIG)

    print(f"cairn: unknown trigger action {args.action!r}", file=sys.stderr)
    return int(ExitCode.CONFIG)


def _trigger_status_json(status: TriggerStatus) -> dict[str, Any]:
    return {
        "name": status.name,
        "declared": status.declared,
        "installed": status.installed,
        "stuck": [str(p) for p in status.stuck],
    }


def _print_trigger_list(statuses: list[TriggerStatus], backend: str) -> None:
    print(f"cairn: triggers (declared vs installed on {backend}):")
    if not statuses:
        print("  (none)")
    for s in statuses:
        if s.declared and s.installed:
            note = "= in sync"
        elif s.declared:
            note = "+ declared, not installed"
        else:
            note = "- installed, not declared"
        print(f"  {note:32} {s.name}")
        for claim_path in s.stuck:
            print(f"      ! stuck claim (never auto-retried): {claim_path}")


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
    sp.add_argument(
        "--force", action="store_true",
        help="accept pipeline-hash/version drift (re-pins run.json so later resumes don't need it)",
    )
    sp.add_argument(
        "--from", dest="from_node", metavar="NODE",
        help="re-execute from this node: its and every later node's records are cleared and "
             "their existing artifacts moved to superseded/<stamp>/ before the walk",
    )
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

    # batch — a pool of `cairn run --headless` children over a JSONL params file
    sp = sub.add_parser("batch", help="run a pipeline over every line of a JSONL params file")
    sp.add_argument("pipeline")
    sp.add_argument("--params-file", required=True, metavar="FILE", help="JSONL: one param object per line")
    sp.add_argument("-j", "--jobs", type=int, default=4, help="max concurrent child runs (default 4)")
    sp.add_argument("--gate", action="append", metavar="NAME=CHOICE", help="preset a gate for every child (repeatable)")
    sp.add_argument("--to", help="stop each child run at this node (pass-through to `cairn run`)")
    sp.add_argument("--from", dest="from_node", help="start each child run at this node (pass-through to `cairn run`)")
    sp.set_defaults(func=_cmd_batch)

    # learnings — aggregate `learn` trail events across every run
    sp = sub.add_parser("learnings", help="aggregate learn events across all runs")
    sp.add_argument("--since", metavar="DATE", help="only events on/after this ISO date/datetime")
    sp.add_argument("--tag", help="only events with this exact tag")
    sp.set_defaults(func=_cmd_learnings)

    # gc — explicit retention (dry-run by default; --apply deletes)
    sp = sub.add_parser("gc", help="retention over the runs root (dry-run unless --apply)")
    sp.add_argument("--keep-days", type=int, metavar="N", help="delete runs older than N days")
    sp.add_argument("--keep-last", type=int, metavar="M", help="keep the newest M runs per pipeline")
    sp.add_argument("--artifacts-only", action="store_true", help="slim runs to the audit skeleton, not delete")
    sp.add_argument("--include-needs-human", action="store_true", help="also consider gate/needs-human runs")
    sp.add_argument("--apply", action="store_true", help="actually delete/slim (default is a dry-run plan)")
    sp.set_defaults(func=_cmd_gc)

    # schedule — sync schedules.yaml into the host scheduler
    sp = sub.add_parser("schedule", help="sync schedules.yaml → host scheduler (install/list/run/uninstall)")
    sp.add_argument("action", choices=["install", "list", "run", "uninstall"])
    sp.add_argument("name", nargs="?", help="schedule name (required for `run`)")
    sp.add_argument("--backend", choices=["cron", "launchd", "systemd"], default="cron", help="host backend (default cron)")
    sp.add_argument("--launchd-dir", help="override the launchd LaunchAgents dir")
    sp.add_argument("--systemd-dir", help="override the systemd user-unit dir")
    sp.set_defaults(func=_cmd_schedule)

    # trigger — sync triggers.yaml into the host watcher
    sp = sub.add_parser("trigger", help="sync triggers.yaml → host watcher (sync/list/remove/run)")
    sp.add_argument("action", choices=["sync", "list", "remove", "run"])
    sp.add_argument("name", nargs="?", help="trigger name (required for remove/run)")
    sp.add_argument("--backend", choices=["cron", "launchd", "systemd"], default="cron", help="host backend (default cron)")
    sp.add_argument("--launchd-dir", help="override the launchd LaunchAgents dir")
    sp.add_argument("--systemd-dir", help="override the systemd user-unit dir")
    sp.add_argument("--workspace", default=".", help="workspace root (default .)")
    sp.add_argument("--json", action="store_true", help="list: machine-readable output")
    sp.add_argument(
        "--headless", action="store_true",
        help="accepted, ignored — a fired trigger's child run is already --headless by "
             "construction; present so the documented cron-fallback schedules.yaml entry "
             "(`run: [trigger, run, <name>, --headless]`, TRIGGERS.md §3) is copy-paste invocable",
    )
    sp.set_defaults(func=_cmd_trigger)

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
