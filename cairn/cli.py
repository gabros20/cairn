"""cairn CLI — the argparse frame plus the thin wiring that turns the kernel into a tool.

Every subcommand parses its flags, wires kernel objects together, and prints; the logic
stays in the kernel. Verbs (docs/API.md §9): plan/run/resume/gate/validate/trail/ps/inbox/
doctor/test/compose/new plus the fleet verbs batch/learnings/gc/schedule/trigger are all
live — each is a thin binding over its kernel module (batchkit/learnkit/gckit/schedkit/
triggerkit).

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
from cairn.kernel.config import Config, ExecutorConfig, installed_version, load_config
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
    _iter_gates,
    plan as build_plan,
    resolve_lane,
)
from cairn.kernel.proc import SubprocessRunner as _SubprocessRunner
from cairn.kernel.gckit import write_queue_pin
from cairn.kernel.queue_ledger import (
    RetryError,
    RetryPrepared,
    RetryRefused,
    pointer_path,
    prepare_failed_retry,
    read_pointer,
    reset_circuit,
    retire,
    sweep,
)
from cairn.kernel.runctl import (
    AlreadyDone,
    Minted,
    Refusal,
    RefusalKind,
    Resumable,
    preflight_tools,
    resolve_run,
    resume_existing,
    workspace_git_rev,
)
from cairn.kernel.runstate import LockHeldError, load_run, run_lock, update_run
from cairn.kernel.schedkit import (
    diff_schedules,
    install as install_schedules,
    list_installed,
    load_schedules,
    run_schedule,
    uninstall as uninstall_schedules,
)
from cairn.kernel.trail import RunStatus, derive_status, follow, format_at, read_trail
from cairn.kernel.fssafety import check_watch_fs_safety
from cairn.kernel.trigger_host import load_triggers, watch_dir
from cairn.kernel.triggerkit import (
    TriggerStatus,
    list_installed_triggers,
    reconcile_workspace,
    remove_trigger,
    run_trigger,
    sync_triggers,
)
from cairn.kernel.types import ExitCode, Finding, classify_exit
from cairn.kernel.walk import invalidate_from, walk
from cairn.kernel.wsid import label_prefix_for, workspace_id

# The full verb surface (docs/API.md §9). Order is the help-listing order.
SUBCOMMANDS: list[str] = [
    "plan",
    "run",
    "resume",
    "gate",
    "validate",
    "trail",
    "ps",
    "inbox",
    "doctor",
    "test",
    "new",
    "compose",
    "batch",
    "learnings",
    "gc",
    "schedule",
    "trigger",
    "factory",
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
    gate_preset_by: dict[str, str] | None = None,
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
        gate_preset_by=gate_preset_by,
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


def _print_refusal(r: Refusal) -> int:
    """Adapter: print advisories then Refusal.message (old inline order) to stderr."""
    _print_advisories(r.advisories)
    print(r.message, file=sys.stderr)
    return int(r.code)


def _print_advisories(lines: tuple[str, ...]) -> None:
    """Adapter: print runctl advisories to stderr in collected order (CLI owns presentation)."""
    for line in lines:
        print(line, file=sys.stderr)


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
        # W5: resolve --lane BEFORE mint so an unknown lane / --gate conflict refuses
        # with nothing on disk (matches every other plan-time ConfigError). Needs only
        # the Plan — never the run dir. No-lane path is byte-identical (D7).
        p, gate_presets, gate_preset_by = resolve_lane(
            p, getattr(args, "lane", None), _kv(args.gate)
        )
    except ConfigError as exc:
        return _print_config_error(exc)

    phash = _pipeline_hash(ws, args.pipeline)
    runs_root = _runs_root(ws, config)
    # Entrance decision tree lives in runctl (W0.2): --run-dir / --idempotent / fresh mint.
    outcome = resolve_run(
        ws,
        p,
        now=now,
        pipeline_hash=phash,
        runs_root=runs_root,
        run_dir=args.run_dir,
        idempotent=bool(args.idempotent),
    )
    if isinstance(outcome, Refusal):
        return _print_refusal(outcome)
    if isinstance(outcome, AlreadyDone):
        print(f"cairn: already done → {outcome.run_dir}")
        return int(ExitCode.OK)

    run_dir = outcome.run_dir
    if isinstance(outcome, Minted):
        _print_advisories(outcome.advisories)
        # Record the actual executor routing so `cairn resume` reconstructs the same fleet
        # (mixed --executor / --step-executor) instead of silently falling back to defaults.
        global_default = (None if stub_mode else args.executor) or config.workspace.default_executor or ""
        _record_executor_routing(run_dir, now, global_default, _kv(args.step_executor), p.resolved_models, ws)
    elif isinstance(outcome, Resumable):
        _print_advisories(outcome.advisories)

    # Origin provenance (W2): drain stamps --origin trigger:<name>; inbox groups/sweeps by it.
    # Schema root allows additional properties; origin is an extra field on run.json.
    origin = getattr(args, "origin", None)
    if origin:
        update_run(run_dir, lambda doc: doc.__setitem__("origin", origin))

    interactive = (not args.headless) and sys.stdin.isatty()
    try:
        return _drive(
            p, run_dir, ws, config,
            interactive=interactive, gate_presets=gate_presets, now=now, stub_mode=stub_mode,
            gate_preset_by=gate_preset_by,
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
    git = workspace_git_rev(ws)

    def mutate(doc: dict) -> None:
        ex = doc.setdefault("executors", {})
        ex["default"] = default
        ex["overrides"] = dict(overrides)
        ex["versions"] = versions
        doc["git_rev"] = git["rev"] if git is not None else None
        doc["git_dirty"] = git["dirty"] if git is not None else None

    update_run(run_dir, mutate)


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


def _drive_resume(
    ws: Path,
    run_dir: Path,
    run_doc: dict,
    *,
    force: bool = False,
    from_node: str | None = None,
    interactive: bool | None = None,
    now: datetime | None = None,
    phash: str | None = None,
) -> int:
    """Shared post-guard resume drive — ONE path for ``cairn resume`` and ``cairn inbox`` (D10).

    Callers must already have passed :func:`resume_existing` and printed advisories.
    This owns --force re-pin, plan rebuild from the recorded fleet, tool preflight,
    optional ``--from`` invalidation, and the walk. Returns an exit code.
    """
    now = now if now is not None else _now()
    pipeline = run_doc["pipeline"]
    if phash is None:
        pfile = ws / "pipelines" / f"{pipeline}.yaml"
        if not pfile.is_file():
            print(f"cairn: cannot resume — pipeline file {pfile} no longer exists", file=sys.stderr)
            return int(ExitCode.CONFIG)
        phash = _pipeline_hash(ws, pipeline)
    if force:
        _repin_manifest(run_dir, run_doc, phash)

    params = {k: _param_str(v) for k, v in (run_doc.get("params") or {}).items() if v is not None}
    # Reconstruct the recorded fleet (§8.1 executors) so a mixed-executor run resumes on the
    # same models, not the workspace defaults.
    ex_doc = run_doc.get("executors") or {}
    recorded_default = ex_doc.get("default") or None
    recorded_overrides = {k: str(v) for k, v in (ex_doc.get("overrides") or {}).items()}
    try:
        p = build_plan(
            ws, pipeline, params, executor=recorded_default, step_executors=recorded_overrides, now=now
        )
        config = load_config(ws)
    except ConfigError as exc:
        return _print_config_error(exc)

    # Resume re-checks in-range tools: a tool can vanish between sessions and the steps ahead
    # still need it (docs/TOOLING-AND-GROWTH §2). Cheap, and nothing is walked on a failure.
    refused = preflight_tools(p)
    if refused is not None:
        return _print_refusal(refused)

    if from_node:
        try:
            cleared, moved = invalidate_from(p, run_dir, from_node, now=now)
        except ConfigError as exc:
            return _print_config_error(exc)
        except CairnError as exc:  # LockHeldError — another process holds the run
            print(f"cairn: {exc}", file=sys.stderr)
            return int(ExitCode.EXECUTOR)
        print(
            f"cairn: --from {from_node!r} — cleared {cleared} node record(s), "
            f"superseded {moved} artifact file(s)",
            file=sys.stderr,
        )

    if interactive is None:
        interactive = sys.stdin.isatty()
    try:
        return _drive(p, run_dir, ws, config, interactive=interactive, gate_presets={}, now=now)
    except CairnError as exc:
        print(f"cairn: {exc}", file=sys.stderr)
        return int(ExitCode.EXECUTOR)


def _cmd_resume(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    ws = _workspace(args)
    now = _now()
    # Pipeline name is needed before the file-exists check; load once for that, then
    # resume_existing reloads fail-loud through the shared guard path (W0.2).
    try:
        run_doc = load_run(run_dir)
    except (OSError, ValueError, ConfigError) as exc:
        print(f"cairn: cannot read {run_dir}/run.json: {exc}", file=sys.stderr)
        return int(ExitCode.CONFIG)

    pipeline = run_doc["pipeline"]
    pfile = ws / "pipelines" / f"{pipeline}.yaml"
    if not pfile.is_file():
        print(f"cairn: cannot resume — pipeline file {pfile} no longer exists", file=sys.stderr)
        return int(ExitCode.CONFIG)

    phash = _pipeline_hash(ws, pipeline)
    outcome = resume_existing(
        run_dir, ws=ws, phash=phash, pipeline=pipeline, force=bool(args.force)
    )
    if isinstance(outcome, Refusal):
        return _print_refusal(outcome)
    # Advisories (force-override / version / repro) print here — same position as the
    # former in-guard stderr writes, before --force re-pin.
    _print_advisories(outcome.advisories)
    return _drive_resume(
        ws,
        run_dir,
        outcome.run_doc,
        force=bool(args.force),
        from_node=args.from_node,
        interactive=sys.stdin.isatty(),
        now=now,
        phash=phash,
    )


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


def _age_secs(at: str | None) -> float:
    """Numeric age in seconds for oldest-first sorting; missing/unparseable → 0."""
    if not at:
        return 0.0
    try:
        then = datetime.fromisoformat(at.replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - then).total_seconds())
    except (ValueError, TypeError):
        return 0.0


# --------------------------------------------------------------------------- #
# inbox — the cross-run judgment drain (FACTORY-PLAN W2)
# --------------------------------------------------------------------------- #


class _InboxAbort(Exception):
    """EOF / Ctrl-C during an inbox prompt — abort the whole command (not per-card skip)."""


@dataclasses.dataclass
class _InboxCard:
    """One parked judgment the human may answer (or a non-answerable wait note)."""

    run_dir: Path
    run_id: str
    pipeline: str
    kind: str  # gate | manual | waiting_other
    node: str
    question: str
    options: list[str]
    manual_text: str | None
    produces: list[str]
    reads: list[dict[str, Any]]  # {name, path, size, mtime}
    age: str
    age_secs: float
    origin: str | None
    waiting_kind: str | None
    at: str | None


@dataclasses.dataclass(frozen=True)
class _InboxTrailView:
    """Single-pass trail summary for inbox enumeration (status + terminal + pending)."""

    status: RunStatus
    term_kind: str  # done | halt | none
    exit_code: int | None
    last_pending: dict | None


def _scan_trail_inbox(run_dir: Path, *, grace: int | None) -> _InboxTrailView:
    """One trail read: derive_status + last_trail_terminal + last gate-pending.

    Before (per run): up to 3 full ``read_trail`` scans. After: exactly 1.
    """
    last: dict | None = None
    term_kind = "none"
    exit_code: int | None = None
    last_pending: dict | None = None
    for ev in read_trail(run_dir):
        last = ev
        event = ev.get("event")
        if event == "run-done":
            term_kind = "done"
            exit_code = None
        elif event == "run-halt":
            term_kind = "halt"
            data = ev.get("data") or {}
            raw = data.get("exit_code")
            try:
                exit_code = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                exit_code = None
        if event == "gate-pending":
            last_pending = ev

    # Same classification as trail.derive_status, without a second scan.
    if last is None:
        st = RunStatus(status="stale", last_event=None, node=None)
    else:
        node = last.get("node")
        event = last.get("event")
        if event == "gate-pending":
            st = RunStatus(status="gate", last_event=last, node=node)
        elif event == "run-halt":
            st = RunStatus(status="halted", last_event=last, node=node)
        elif event == "run-done":
            st = RunStatus(status="done", last_event=last, node=node)
        elif grace is not None:
            try:
                age_s = (datetime.now(timezone.utc) - datetime.fromisoformat(
                    str(last["at"]).replace("Z", "+00:00")
                )).total_seconds()
            except (KeyError, ValueError, TypeError):
                age_s = None
            if age_s is not None and age_s > grace:
                st = RunStatus(status="stale", last_event=last, node=node)
            else:
                st = RunStatus(status="running", last_event=last, node=node)
        else:
            st = RunStatus(status="running", last_event=last, node=node)

    return _InboxTrailView(
        status=st, term_kind=term_kind, exit_code=exit_code, last_pending=last_pending
    )


def _reads_for_gate(
    ws: Path, run_doc: dict, gate_name: str, run_dir: Path, now: datetime
) -> list[dict[str, Any]]:
    """Resolve a gate's ``reads`` artifacts to path/size/mtime (best-effort)."""
    pipeline = run_doc.get("pipeline")
    if not isinstance(pipeline, str):
        return []
    params = {k: _param_str(v) for k, v in (run_doc.get("params") or {}).items() if v is not None}
    try:
        p = build_plan(ws, pipeline, params, now=now)
    except ConfigError:
        return []
    gates = {g.name: g for g in _iter_gates(p.nodes)}
    gate = gates.get(gate_name)
    if gate is None:
        return []
    out: list[dict[str, Any]] = []
    for name in gate.reads:
        decl = p.artifacts.get(name)
        if decl is None:
            out.append({"name": name, "path": None, "size": None, "mtime": None})
            continue
        try:
            rendered = render_artifact_path(
                decl, params=p.params, dims=p.dims, pipeline=p.pipeline, cycle=None, now=now
            )
            resolved = resolve_path(decl, rendered, run_dir)
        except (OSError, ValueError, ConfigError, CairnError):
            out.append({"name": name, "path": None, "size": None, "mtime": None})
            continue
        path = resolved.paths[0] if resolved.paths else run_dir / rendered
        size = mtime = None
        if path.is_file():
            try:
                st = path.stat()
                size = st.st_size
                mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
            except OSError:
                pass
        out.append({"name": name, "path": str(path), "size": size, "mtime": mtime})
    return out


def _card_from_run(
    ws: Path,
    run_dir: Path,
    run_doc: dict,
    *,
    now: datetime,
    grace: int | None,
    load_reads: bool = True,
) -> _InboxCard | None:
    """Build an inbox card for a run that needs human judgment, else None.

    Answerable: derive_status == ``gate``, or ``halted`` with last terminal exit 6.
    Capacity(8)/blocked(9) return a ``waiting_other`` card (listed, not answerable).
    ``load_reads``: pipeline re-parse for gate reads[] — skip in --list (not rendered).
    """
    view = _scan_trail_inbox(run_dir, grace=grace)
    st = view.status
    origin = run_doc.get("origin") if isinstance(run_doc.get("origin"), str) else None
    run_id = str(run_doc.get("run_id") or run_dir.name)
    pipeline = str(run_doc.get("pipeline") or "?")

    answerable = False
    waiting_kind: str | None = None
    if st.status == "gate":
        answerable = True
    elif st.status == "halted" and view.term_kind == "halt" and view.exit_code is not None:
        outcome = classify_exit(view.exit_code)
        if outcome.waiting_kind == "needs_human":
            answerable = True
            waiting_kind = "needs_human"
        elif outcome.waiting_kind in ("capacity", "blocked"):
            waiting_kind = outcome.waiting_kind
            last = st.last_event or {}
            at = last.get("at") if isinstance(last.get("at"), str) else None
            return _InboxCard(
                run_dir=run_dir,
                run_id=run_id,
                pipeline=pipeline,
                kind="waiting_other",
                node=str(st.node or "-"),
                question=f"waiting ({waiting_kind}) — not yours to answer here",
                options=[],
                manual_text=None,
                produces=[],
                reads=[],
                age=_age(at),
                age_secs=_age_secs(at),
                origin=origin,
                waiting_kind=waiting_kind,
                at=at,
            )
    if not answerable:
        return None

    pending = view.last_pending
    data = (pending.get("data") or {}) if pending else {}
    node = str((pending or {}).get("node") or st.node or "-")
    at = (pending or st.last_event or {}).get("at")
    at_s = at if isinstance(at, str) else None

    # Manual parks share the gate-pending event with data.manual (walk._run_manual).
    if isinstance(data.get("manual"), str):
        produces = [str(x) for x in (data.get("produces") or [])]
        return _InboxCard(
            run_dir=run_dir,
            run_id=run_id,
            pipeline=pipeline,
            kind="manual",
            node=node,
            question="manual step requires an operator",
            options=[],
            manual_text=data["manual"],
            produces=produces,
            reads=[],
            age=_age(at_s),
            age_secs=_age_secs(at_s),
            origin=origin,
            waiting_kind=waiting_kind or "needs_human",
            at=at_s,
        )

    # Gate: question/options on the pending event; reads only when rendered (interactive/json).
    question = str(data.get("question") or f"gate {node} needs a decision")
    options = [str(o) for o in (data.get("options") or [])]
    reads = _reads_for_gate(ws, run_doc, node, run_dir, now) if load_reads else []
    return _InboxCard(
        run_dir=run_dir,
        run_id=run_id,
        pipeline=pipeline,
        kind="gate",
        node=node,
        question=question,
        options=options,
        manual_text=None,
        produces=[],
        reads=reads,
        age=_age(at_s),
        age_secs=_age_secs(at_s),
        origin=origin,
        waiting_kind=waiting_kind or "needs_human",
        at=at_s,
    )


def _enumerate_inbox(
    ws: Path,
    runs_root: Path,
    *,
    grace: int | None,
    now: datetime,
    load_reads: bool = True,
) -> tuple[list[_InboxCard], list[_InboxCard]]:
    """Walk runs_root; return (answerable oldest-first, waiting_other)."""
    answerable: list[_InboxCard] = []
    other: list[_InboxCard] = []
    if not runs_root.is_dir():
        return answerable, other
    for d in sorted(runs_root.iterdir()):
        if not d.is_dir() or not (d / "run.json").is_file():
            continue
        try:
            doc = load_run(d)
        except (OSError, ValueError, ConfigError):
            continue
        card = _card_from_run(ws, d, doc, now=now, grace=grace, load_reads=load_reads)
        if card is None:
            continue
        if card.kind == "waiting_other":
            other.append(card)
        else:
            answerable.append(card)
    # Oldest-first (largest age_secs first); stable secondary key for determinism.
    answerable.sort(key=lambda c: (-c.age_secs, c.run_id))
    return answerable, other


def _render_card(card: _InboxCard) -> None:
    print("─" * 56)
    print(f"pipeline · {card.pipeline}")
    print(f"run      · {card.run_dir}")
    print(f"gate     · {card.node}  ({card.kind})")
    if card.kind == "manual":
        print(f"instruction · {card.manual_text}")
        print(f"produces · {', '.join(card.produces) or '(none)'}")
        print("(do the work, then confirm with Enter to resume — validators own done)")
    else:
        print(f"question · {card.question}")
        if card.options:
            print(f"options  · {' / '.join(card.options)}")
    if card.reads:
        print("reads:")
        for r in card.reads:
            size = f"{r['size']}B" if r.get("size") is not None else "?"
            print(f"  - {r.get('name')}: {r.get('path') or '?'} ({size})")
    print(f"age      · {card.age}")
    print(f"origin   · {card.origin or '-'}")


def _prompt(msg: str) -> str:
    """Read a line; EOF / Ctrl-C abort the whole inbox (``s`` is the explicit skip)."""
    try:
        return input(msg).strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise _InboxAbort() from exc


def _sweep_origin(ws: Path, origin: str | None) -> None:
    """After a successful resume, retire the origin trigger's waiting item if any."""
    if not origin or not origin.startswith("trigger:"):
        return
    name = origin[len("trigger:") :]
    if not name:
        return
    try:
        triggers = load_triggers(ws)
    except ConfigError as exc:
        print(f"cairn: inbox: cannot load triggers for origin sweep: {exc}", file=sys.stderr)
        return
    trigger = triggers.get(name)
    if trigger is None:
        print(f"cairn: inbox: origin trigger {name!r} not declared — skip sweep", file=sys.stderr)
        return
    try:
        watch_abs = watch_dir(trigger, ws)
        report = sweep(watch_abs, on_done=trigger.on_done)
    except (OSError, ConfigError, CairnError) as exc:
        print(f"cairn: inbox: origin sweep for {name!r} failed: {exc}", file=sys.stderr)
        return
    if report.moved:
        names = ", ".join(p.name for p in report.moved)
        print(f"cairn: inbox: origin sweep retired {names} under {trigger.watch}")
    for d in report.diagnostics:
        print(f"cairn: inbox: sweep note: {d}", file=sys.stderr)


def _inbox_resume_card(ws: Path, card: _InboxCard, *, force: bool, now: datetime) -> int:
    """Call resume_existing + shared _drive_resume; handle DRIFT card (never auto-force)."""
    run_dir = card.run_dir
    try:
        run_doc = load_run(run_dir)
    except (OSError, ValueError, ConfigError) as exc:
        print(f"cairn: cannot read {run_dir}/run.json: {exc}", file=sys.stderr)
        return int(ExitCode.CONFIG)
    pipeline = run_doc["pipeline"]
    pfile = ws / "pipelines" / f"{pipeline}.yaml"
    if not pfile.is_file():
        print(f"cairn: cannot resume — pipeline file {pfile} no longer exists", file=sys.stderr)
        return int(ExitCode.CONFIG)
    phash = _pipeline_hash(ws, pipeline)
    outcome = resume_existing(run_dir, ws=ws, phash=phash, pipeline=pipeline, force=force)
    if isinstance(outcome, Refusal):
        if outcome.kind is RefusalKind.DRIFT and not force:
            print("─" * 56)
            print("DRIFT CARD — pipeline changed since this run started")
            print(outcome.message)
            print("Auto-force is never applied. Type 'f' to force-resume, anything else to leave parked.")
            choice = _prompt("force? [f/s]: ").lower()
            if choice == "f":
                return _inbox_resume_card(ws, card, force=True, now=now)
            print(f"cairn: inbox: left parked → {run_dir}")
            return int(outcome.code)
        _print_refusal(outcome)
        return int(outcome.code)
    _print_advisories(outcome.advisories)
    # Inbox resumes are non-interactive for gates (already answered) and for manuals
    # (produces already written — interactive manual would re-prompt).
    code = _drive_resume(
        ws,
        run_dir,
        outcome.run_doc,
        force=force,
        from_node=None,
        interactive=False,
        now=now,
        phash=phash,
    )
    if code == int(ExitCode.OK):
        origin = outcome.run_doc.get("origin") or card.origin
        _sweep_origin(ws, origin if isinstance(origin, str) else None)
    return code


def _recorded_gate_choice_repr(ws: Path, run_dir: Path, gate_name: str, *, now: datetime) -> str:
    """Best-effort quoted choice for the already-answered note (never echo unverified)."""
    try:
        run_doc = load_run(run_dir)
        params = {
            k: _param_str(v) for k, v in (run_doc.get("params") or {}).items() if v is not None
        }
        p = build_plan(ws, run_doc["pipeline"], params, now=now)
        gate = {g.name: g for g in _iter_gates(p.nodes)}.get(gate_name)
        if gate is None:
            return "recorded"
        return repr(read_verified_choice(run_dir, gate))
    except (OSError, ValueError, ConfigError, GateTampered, GateUnanswered, CairnError):
        return "recorded"


def _inbox_answer_and_resume(ws: Path, card: _InboxCard, choice: str, *, now: datetime) -> int:
    """Answer a gate (or confirm a manual) under the run lock, then shared resume.

    C1: never clobber a signed decision; concurrent inbox/drain → skip this card.
    Lock is held only for the answer phase (walk takes its own lock on resume).
    """
    try:
        with run_lock(card.run_dir):
            if card.kind == "gate":
                if is_answered(card.run_dir, card.node):
                    # Mirror _cmd_gate: never silent-overwrite. Resume on the recorded choice.
                    recorded = _recorded_gate_choice_repr(ws, card.run_dir, card.node, now=now)
                    print(
                        f"cairn: inbox: gate {card.node!r} already answered ({recorded}) — resuming"
                    )
                else:
                    if choice not in card.options:
                        print(
                            f"cairn: {choice!r} is not an option "
                            f"(options: {', '.join(card.options)})",
                            file=sys.stderr,
                        )
                        return int(ExitCode.CONFIG)
                    answer_gate(card.run_dir, card.node, choice)
                    print(f"cairn: gate {card.node!r} answered {choice!r} (by external)")
            # manual: human already produced; no answer_gate — validators own done on resume.
    except LockHeldError:
        print(
            f"cairn: inbox: run busy (a drain or another inbox is on it) — skipping {card.run_id}",
            file=sys.stderr,
        )
        return int(ExitCode.OK)
    return _inbox_resume_card(ws, card, force=False, now=now)


def _list_failed(ws: Path) -> list[dict[str, Any]]:
    """Read-only listing of ``.failed/`` items that carry a run pointer.

    Action surface: ``cairn trigger retry <trigger> <item>`` (W4 SG3).
    """
    rows: list[dict[str, Any]] = []
    try:
        triggers = load_triggers(ws)
    except ConfigError:
        return rows
    for name, trigger in sorted(triggers.items()):
        try:
            watch_abs = watch_dir(trigger, ws)
        except ConfigError:
            continue
        failed = watch_abs / ".failed"
        if not failed.is_dir():
            continue
        for item in sorted(p for p in failed.iterdir() if p.is_file() and not p.name.startswith(".")):
            ptr = pointer_path(failed, item.name)
            run_ptr = None
            if ptr.is_file():
                try:
                    rec = read_pointer(ptr)
                    run_ptr = rec.get("run_dir")
                except (OSError, ValueError):
                    run_ptr = "(unreadable pointer)"
            rows.append(
                {
                    "trigger": name,
                    "item": item.name,
                    "watch": trigger.watch,
                    "run_dir": run_ptr,
                    "note": "cairn trigger retry <trigger> <item> resumes these",
                }
            )
    return rows


def _cmd_inbox(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    now = _now()
    try:
        config = load_config(ws)
    except ConfigError as exc:
        return _print_config_error(exc)
    runs_root = _runs_root(ws, config)
    grace = config.defaults.heartbeat_s

    if args.failed:
        rows = _list_failed(ws)
        if args.json:
            print(json.dumps(rows, indent=2))
            return int(ExitCode.OK)
        if not rows:
            print("cairn: no failed items with run pointers")
            return int(ExitCode.OK)
        print(f"{'TRIGGER':20} {'ITEM':28} RUN_DIR")
        for r in rows:
            print(f"{r['trigger']:20} {r['item']:28} {r['run_dir'] or '-'}")
        print(
            "note: listing only — use `cairn trigger retry <trigger> <item>` to "
            "resume a failed item; inbox does not retry."
        )
        return int(ExitCode.OK)

    # --list text does not render reads[] → skip pipeline parse; --json keeps reads.
    list_mode = bool(args.list or args.json or not sys.stdout.isatty())
    load_reads = bool(args.json) or not list_mode
    answerable, other = _enumerate_inbox(
        ws, runs_root, grace=grace, now=now, load_reads=load_reads
    )

    if list_mode:
        if args.json:
            payload = [
                {
                    "run": c.run_id,
                    "run_dir": str(c.run_dir),
                    "pipeline": c.pipeline,
                    "gate": c.node,
                    "kind": c.kind,
                    "age": c.age,
                    "origin": c.origin,
                    "question": c.question,
                    "options": c.options,
                    "produces": c.produces,
                    "manual": c.manual_text,
                    "reads": c.reads,
                }
                for c in answerable
            ]
            if other:
                payload.extend(
                    {
                        "run": c.run_id,
                        "run_dir": str(c.run_dir),
                        "pipeline": c.pipeline,
                        "gate": c.node,
                        "kind": c.kind,
                        "age": c.age,
                        "origin": c.origin,
                        "waiting_kind": c.waiting_kind,
                        "note": "waiting (not yours to answer)",
                    }
                    for c in other
                )
            print(json.dumps(payload, indent=2))
            return int(ExitCode.OK)
        if not answerable and not other:
            print("cairn: inbox empty")
            return int(ExitCode.OK)
        print(f"{'RUN':30} {'PIPELINE':16} {'GATE':14} {'AGE':6} ORIGIN")
        for c in answerable:
            print(
                f"{c.run_id:30} {c.pipeline:16} {c.node:14} {c.age:6} {c.origin or '-'}"
            )
        if other:
            print("waiting (not yours to answer) — capacity/blocked resume on capacity/auth (W3):")
            for c in other:
                print(
                    f"{c.run_id:30} {c.pipeline:16} {c.node:14} {c.age:6} "
                    f"{c.waiting_kind or '-'} {c.origin or '-'}"
                )
        return int(ExitCode.OK)

    # Interactive (stdout is a TTY and neither --list nor --json).
    if not answerable:
        print("cairn: inbox empty")
        if other:
            print("waiting (not yours to answer):")
            for c in other:
                print(f"  {c.run_id}  {c.waiting_kind}  age={c.age}")
        return int(ExitCode.OK)

    try:
        for card in answerable:
            while True:
                _render_card(card)
                if card.kind == "manual":
                    raw = _prompt("ready? [Enter=resume / s=skip / o=open]: ")
                    low = raw.lower()
                    if low in ("s", "skip"):
                        print(f"cairn: inbox: skipped {card.run_id}")
                        break
                    if low in ("o", "open"):
                        print(f"(manual instruction)\n{card.manual_text}")
                        print(f"produces: {', '.join(card.produces) or '(none)'}")
                        print(f"run dir: {card.run_dir}")
                        continue
                    # Enter or any other confirm → resume (human did the work).
                    code = _inbox_answer_and_resume(ws, card, "", now=now)
                    if code != int(ExitCode.OK):
                        print(
                            f"cairn: inbox: resume exited {code} → {card.run_dir}",
                            file=sys.stderr,
                        )
                    break
                # Gate card.
                opt_hint = "/".join(card.options) if card.options else "?"
                raw = _prompt(f"choice [{opt_hint}] (s=skip, o=open): ")
                low = raw.lower()
                if low in ("s", "skip"):
                    print(f"cairn: inbox: skipped {card.run_id}")
                    break
                if low in ("o", "open"):
                    if card.reads:
                        for r in card.reads:
                            print(f"  {r.get('name')}: {r.get('path') or '?'}")
                    else:
                        print("(no reads)")
                    continue
                # Already-answered gates resume without re-validating the typed choice
                # (C1: clobber guard ignores B and keeps A). Fresh gates need a real option.
                if not is_answered(card.run_dir, card.node) and card.options and raw not in card.options:
                    print(f"{raw!r} is not one of {card.options} (or s/o)", file=sys.stderr)
                    continue
                code = _inbox_answer_and_resume(ws, card, raw, now=now)
                if code != int(ExitCode.OK):
                    print(
                        f"cairn: inbox: resume exited {code} → {card.run_dir}",
                        file=sys.stderr,
                    )
                break
    except _InboxAbort:
        # Ctrl-C / EOF: clean exit 0 (same posture as trail --watch / --follow).
        return int(ExitCode.OK)
    return int(ExitCode.OK)


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
        if target == "source":
            result = newkit.new_source(name, _workspace(args))
            print(f"cairn: source {name!r} scaffolded ({len(result.files)} files)")
            for rel in result.files:
                print(f"  + {rel}")
            for note in result.notes:
                print(f"  note: {note}")
            print()
            print(result.triggers_snippet.rstrip())
            print()
            print(result.schedules_snippet.rstrip())
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
    # W3 multi-factory: scope host units under io.cairn.<ws8>.
    prefix = label_prefix_for(ws)
    wid = workspace_id(ws)

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
                label_prefix=prefix,
                ws_id=wid,
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
                label_prefix=prefix,
                ws_id=wid,
            )
            print(f"cairn: removed cairn-managed schedules from the {backend} backend")
            return int(ExitCode.OK)

        if args.action == "list":
            schedules = load_schedules(ws)
            installed = list_installed(
                backend,
                runner=runner,
                launchd_dir=launchd_dir,
                systemd_dir=systemd_dir,
                label_prefix=prefix,
                ws_id=wid,
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


def _refuse_unsafe_watch_dirs(
    workspace_dir: Path,
    *,
    unsafe_synced_fs: bool,
    trigger_names: list[str] | None = None,
) -> int | None:
    """D2 hard refusal when a watch/ledger dir is on cloud-sync or fails hardlink.

    Returns an exit code to abort with, or None to proceed. ``--unsafe-synced-fs``
    logs a warning and proceeds.
    """
    try:
        triggers = load_triggers(workspace_dir)
    except ConfigError:
        return None
    names = trigger_names if trigger_names is not None else sorted(triggers)
    findings: list[Finding] = []
    for name in names:
        if name not in triggers:
            continue
        try:
            watch_abs = watch_dir(triggers[name], workspace_dir)
        except ConfigError:
            continue
        findings.extend(check_watch_fs_safety(watch_abs))
    if not findings:
        return None
    errors = [f for f in findings if f.level == "error"]
    if not errors:
        return None
    if unsafe_synced_fs:
        for f in errors:
            print(f"cairn: WARNING --unsafe-synced-fs: {f.message}", file=sys.stderr)
        return None
    for f in errors:
        fix = f" → {f.fix}" if f.fix else ""
        print(f"cairn: {f.message}{fix}", file=sys.stderr)
    return int(ExitCode.CONFIG)


def _cmd_trigger_reset(args: argparse.Namespace) -> int:
    """``cairn trigger reset <name>`` — close the dark-lane circuit breaker (W5).

    Clears ``<watch>/.circuit`` consecutive_failures→0 so admission resumes after
    an operator fixes the dark lane. A subsequent DONE dark run also auto-closes.

    Race vs a concurrent drain: last-writer-wins. Reset while a live failing
    streak is still retiring may be immediately re-incremented — expected when
    the lane is still broken (fix first, then reset).
    """
    if not args.name:
        print("cairn: `cairn trigger reset` needs a trigger name", file=sys.stderr)
        return int(ExitCode.CONFIG)
    ws = _workspace(args)
    triggers = load_triggers(ws)
    if args.name not in triggers:
        print(
            f"cairn: no trigger named {args.name!r} in triggers.yaml "
            f"(declared: {', '.join(sorted(triggers)) or '(none)'})",
            file=sys.stderr,
        )
        return int(ExitCode.CONFIG)
    trigger = triggers[args.name]
    if trigger.lane_circuit_failures is None:
        print(
            f"cairn: trigger {args.name!r} has no lane_circuit configured "
            f"(nothing to reset)",
            file=sys.stderr,
        )
        return int(ExitCode.CONFIG)
    watch_abs = watch_dir(trigger, ws)
    reset_circuit(watch_abs)
    print(
        f"cairn: trigger {args.name!r}: lane circuit reset "
        f"(consecutive dark failures → 0; admission resumes)"
    )
    return int(ExitCode.OK)


def _cmd_trigger_retry(args: argparse.Namespace) -> int:
    """``cairn trigger retry <trigger> <item>`` — identity-safe FAILED re-entry (W4 SG3).

    Reacquires the identity reservation (strict), moves the item from ``.failed/``
    back to ``.claim/``, and resumes the recorded run (T4 — never a fresh mint).
    A newer/live rev owning the identity → refuse with a clear diagnostic.
    """
    ws = _workspace(args)
    now = _now()
    if not args.name:
        print("cairn: `cairn trigger retry` needs a trigger name", file=sys.stderr)
        return int(ExitCode.CONFIG)
    item_name = getattr(args, "item", None)
    if not item_name:
        print(
            "cairn: `cairn trigger retry` needs an item name "
            "(from `cairn inbox --failed`)",
            file=sys.stderr,
        )
        return int(ExitCode.CONFIG)
    unsafe = bool(getattr(args, "unsafe_synced_fs", False))
    refused = _refuse_unsafe_watch_dirs(
        ws, unsafe_synced_fs=unsafe, trigger_names=[args.name]
    )
    if refused is not None:
        return refused
    try:
        triggers = load_triggers(ws)
    except ConfigError as exc:
        return _print_config_error(exc)
    trigger = triggers.get(args.name)
    if trigger is None:
        print(f"cairn: no trigger named {args.name!r} in triggers.yaml", file=sys.stderr)
        return int(ExitCode.CONFIG)
    try:
        watch_abs = watch_dir(trigger, ws)
    except ConfigError as exc:
        return _print_config_error(exc)

    prepared = prepare_failed_retry(
        watch_abs,
        item_name,
        identity_mode=trigger.identity,
    )
    if isinstance(prepared, RetryError):
        print(f"cairn: trigger retry: {prepared.message}", file=sys.stderr)
        return int(ExitCode.CONFIG)
    if isinstance(prepared, RetryRefused):
        # Nonzero-but-clean: not a crash, not a successful redrive — supersession.
        print(f"cairn: trigger retry: {prepared.message}", file=sys.stderr)
        return int(ExitCode.CONFIG)
    assert isinstance(prepared, RetryPrepared)

    # Re-pin so gc cannot reap the run while we resume (terminal retire cleared it).
    write_queue_pin(
        prepared.run_dir,
        trigger=trigger.name,
        item=prepared.item_name,
        pinned_at=format_at(now),
    )

    try:
        run_doc = load_run(prepared.run_dir)
    except (OSError, ValueError, ConfigError) as exc:
        print(
            f"cairn: trigger retry: cannot read {prepared.run_dir}/run.json: {exc}",
            file=sys.stderr,
        )
        return int(ExitCode.CONFIG)
    pipeline = run_doc["pipeline"]
    pfile = ws / "pipelines" / f"{pipeline}.yaml"
    if not pfile.is_file():
        print(
            f"cairn: trigger retry: pipeline file {pfile} no longer exists",
            file=sys.stderr,
        )
        return int(ExitCode.CONFIG)
    phash = _pipeline_hash(ws, pipeline)
    outcome = resume_existing(
        prepared.run_dir, ws=ws, phash=phash, pipeline=pipeline, force=False
    )
    if isinstance(outcome, Refusal):
        # Leave the item in .claim/ as a stuck claim with diagnostic — operator
        # can force-resume or discard; do not silently re-fail without a walk.
        print(
            f"cairn: trigger retry: resume refused for {prepared.item_name!r} "
            f"(left in .claim/):",
            file=sys.stderr,
        )
        return _print_refusal(outcome)
    _print_advisories(outcome.advisories)
    print(
        f"cairn: trigger retry: resuming {prepared.item_name!r} → {prepared.run_dir}"
        + (f" (identity {prepared.identity})" if prepared.identity else ""),
    )
    code = _drive_resume(
        ws,
        prepared.run_dir,
        outcome.run_doc,
        force=False,
        from_node=None,
        interactive=False,
        now=now,
        phash=phash,
    )
    # Re-retire by walk exit (D8) — same routing as a drain child.
    run_outcome = classify_exit(code)
    try:
        retire(
            watch_abs,
            prepared.claim_path,
            outcome=run_outcome,
            on_done=trigger.on_done,
            exit_code=code,
            run_dir=prepared.run_dir,
        )
    except Exception as exc:  # noqa: BLE001 — surface, leave claim for audit
        print(
            f"cairn: trigger retry: re-retire hazarded for {prepared.item_name!r} "
            f"(left in .claim/): {exc}",
            file=sys.stderr,
        )
        return int(ExitCode.EXECUTOR)
    if code == int(ExitCode.OK):
        print(f"cairn: trigger retry: done → {prepared.run_dir}")
    return code


def _cmd_trigger(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    backend = args.backend
    runner = _SubprocessRunner()
    # Same target-dir resolution schedule uses (`--launchd-dir`/`--systemd-dir`, same
    # per-user defaults) — triggerkit's sync/list/remove take the identical two knobs.
    launchd_dir, systemd_dir = _schedule_dirs(args)
    unsafe = bool(getattr(args, "unsafe_synced_fs", False))

    try:
        if args.action == "retry":
            return _cmd_trigger_retry(args)

        if args.action == "reset":
            return _cmd_trigger_reset(args)

        if args.action == "run":
            if not args.name:
                print("cairn: `cairn trigger run` needs a trigger name", file=sys.stderr)
                return int(ExitCode.CONFIG)
            refused = _refuse_unsafe_watch_dirs(ws, unsafe_synced_fs=unsafe, trigger_names=[args.name])
            if refused is not None:
                return refused
            # The exit code IS the contract (requirement 3): claim hazards and a failed
            # child both surface here, verbatim — never remapped into an ExitCode member.
            # Thread our real streams through (mirrors `schedule run` above): the
            # Runner-captured child output — halt reasons, hazard diagnostics — must
            # reach the operator, never rot silently.
            return run_trigger(
                args.name,
                ws,
                runner=runner,
                cairn_bin=_resolve_cairn_bin(),
                now=_now(),
                out=sys.stdout,
                err=sys.stderr,
            )

        if args.action == "sync":
            refused = _refuse_unsafe_watch_dirs(ws, unsafe_synced_fs=unsafe)
            if refused is not None:
                return refused
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
        "waiting": status.waiting,
        "failed": status.failed,
        "done": status.done,
        # W3 additive depths + caps (absent caps serialize as null).
        "needs_human": status.needs_human,
        "blocked": status.blocked,
        "capacity": status.capacity,
        "inflight": status.inflight,
        "spool": status.spool,
        "concurrency": status.concurrency,
        "order": status.order,
        "waiting_max": status.waiting_max,
        "blocked_max": status.blocked_max,
        "capacity_max": status.capacity_max,
        "wip_max": status.wip_max,
        "inbox_max": status.inbox_max,
        # W3/T13 lease surface.
        "lease_ttl_s": status.lease_ttl_s,
        "lease_ages_s": list(status.lease_ages_s),
        "expired_live": status.expired_live,
        "missing_lease": status.missing_lease,
        # W5 dark-lane circuit breaker (null threshold = not configured).
        "circuit_failures": status.circuit_failures,
        "circuit_consecutive": status.circuit_consecutive,
        "circuit_open": status.circuit_open,
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
        has_depth = s.waiting or s.failed or s.done or s.stuck or s.spool or s.inflight
        has_caps = any(
            v is not None
            for v in (s.waiting_max, s.blocked_max, s.capacity_max, s.wip_max, s.inbox_max)
        )
        has_lease = s.lease_ttl_s is not None or s.lease_ages_s or s.expired_live
        has_circuit = s.circuit_failures is not None
        if s.declared and (
            has_depth
            or has_caps
            or has_lease
            or has_circuit
            or s.concurrency != 1
            or s.order != "name"
        ):
            print(
                f"      waiting={s.waiting} failed={s.failed} done={s.done} "
                f"stuck={len(s.stuck)}"
            )
            print(
                f"      needs-human={s.needs_human} blocked={s.blocked} "
                f"capacity={s.capacity} inflight={s.inflight} spool={s.spool}"
            )
            caps: list[str] = [f"concurrency={s.concurrency}", f"order={s.order}"]
            if s.waiting_max is not None:
                caps.append(f"waiting_max={s.waiting_max}")
            if s.blocked_max is not None:
                caps.append(f"blocked_max={s.blocked_max}")
            if s.capacity_max is not None:
                caps.append(f"capacity_max={s.capacity_max}")
            if s.wip_max is not None:
                caps.append(f"wip_max={s.wip_max}")
            if s.inbox_max is not None:
                caps.append(f"inbox_max={s.inbox_max}")
            print(f"      {' '.join(caps)}")
            if s.lease_ttl_s is not None or s.lease_ages_s or s.expired_live:
                ages = ",".join(f"{a:.0f}s" for a in s.lease_ages_s) or "-"
                ttl = s.lease_ttl_s if s.lease_ttl_s is not None else "off"
                print(
                    f"      lease_ttl={ttl} ages=[{ages}] "
                    f"expired_live={s.expired_live} missing_lease={s.missing_lease}"
                )
            if has_circuit:
                state = "open" if s.circuit_open else "closed"
                print(
                    f"      circuit={state} consecutive={s.circuit_consecutive}/"
                    f"{s.circuit_failures}"
                )
        for claim_path in s.stuck:
            print(f"      ! stuck claim (never auto-retried): {claim_path}")


def _cmd_factory(args: argparse.Namespace) -> int:
    """``cairn factory reconcile`` — single-flight all-trigger health pass (T13)."""
    ws = _workspace(args)
    if args.action != "reconcile":
        print(f"cairn: unknown factory action {args.action!r}", file=sys.stderr)
        return int(ExitCode.CONFIG)
    unsafe = bool(getattr(args, "unsafe_synced_fs", False))
    refused = _refuse_unsafe_watch_dirs(ws, unsafe_synced_fs=unsafe)
    if refused is not None:
        return refused
    try:
        report = reconcile_workspace(
            ws,
            now=_now(),
            out=sys.stdout,
            err=sys.stderr,
        )
    except ConfigError as exc:
        return _print_config_error(exc)
    if report.already_running:
        return int(ExitCode.OK)
    if report.hazarded:
        return 1
    return int(ExitCode.OK)


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
    sp.add_argument(
        "--lane",
        metavar="NAME",
        help="select a pipeline autonomy lane (resolves its gates: into presets; "
             "conflicts with --gate on the same gate raise ConfigError)",
    )
    sp.add_argument("--headless", action="store_true")
    sp.add_argument("--to")
    sp.add_argument("--from", dest="from_node")
    sp.add_argument("--run-dir")
    sp.add_argument("--idempotent", action="store_true")
    sp.add_argument(
        "--origin",
        metavar="STR",
        help="provenance stamp recorded on run.json (e.g. trigger:<name> from the drain)",
    )
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

    # inbox — cross-run judgment drain (FACTORY-PLAN W2)
    sp = sub.add_parser("inbox", help="cross-run judgment drain (answer gates/manuals, resume)")
    sp.add_argument("--workspace", default=".")
    sp.add_argument("--list", action="store_true", help="one line per parked judgment (non-interactive)")
    sp.add_argument("--json", action="store_true", help="structured listing")
    sp.add_argument(
        "--failed",
        action="store_true",
        help="list .failed/ items with run pointers (read-only; retry via "
             "`cairn trigger retry`)",
    )
    sp.set_defaults(func=_cmd_inbox)

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
    sp = sub.add_parser(
        "new",
        help="scaffold a workspace, single-file stub, or source adapter",
    )
    sp.add_argument(
        "target",
        choices=["workspace", "pipeline", "agent", "skill", "validator", "source"],
    )
    sp.add_argument(
        "name",
        help=(
            "workspace/stub name, or for `source` one of: "
            + ", ".join(newkit.KNOWN_PROVIDERS)
        ),
    )
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

    # trigger — sync triggers.yaml into the host watcher + identity-safe retry
    sp = sub.add_parser(
        "trigger",
        help="sync triggers.yaml → host watcher (sync/list/remove/run/retry/reset)",
    )
    sp.add_argument(
        "action",
        choices=["sync", "list", "remove", "run", "retry", "reset"],
    )
    sp.add_argument(
        "name",
        nargs="?",
        help="trigger name (required for remove/run/retry/reset)",
    )
    sp.add_argument(
        "item",
        nargs="?",
        help="work-item name (required for retry; from `cairn inbox --failed`)",
    )
    sp.add_argument("--backend", choices=["cron", "launchd", "systemd"], default="cron", help="host backend (default cron)")
    sp.add_argument("--launchd-dir", help="override the launchd LaunchAgents dir")
    sp.add_argument("--systemd-dir", help="override the systemd user-unit dir")
    sp.add_argument("--workspace", default=".", help="workspace root (default .)")
    sp.add_argument("--json", action="store_true", help="list: machine-readable output")
    sp.add_argument(
        "--unsafe-synced-fs",
        action="store_true",
        help="override D2 hard refusal when watch/ledger sits on cloud-sync or fails "
             "hard-link probe (logged; data-loss risk — FACTORY-PLAN D2)",
    )
    sp.add_argument(
        "--headless", action="store_true",
        help="accepted, ignored — a fired trigger's child run is already --headless by "
             "construction; present so the documented cron-fallback schedules.yaml entry "
             "(`run: [trigger, run, <name>, --headless]`, TRIGGERS.md §3) is copy-paste invocable",
    )
    sp.add_argument(
        "--lane",
        metavar="NAME",
        help="accepted, ignored — a trigger's lane: is declared in triggers.yaml and "
             "appended to the host-watcher argv; the drain reads it from the Trigger",
    )
    sp.set_defaults(func=_cmd_trigger)

    # factory — host-woken / manual health pass (T13; no daemon — D1)
    sp = sub.add_parser(
        "factory",
        help="factory health verbs (reconcile: sweep all triggers, reap leases, mop deferred)",
    )
    sp.add_argument("action", choices=["reconcile"], help="reconcile: single-flight all-trigger sweep")
    sp.add_argument("--workspace", default=".", help="workspace root (default .)")
    sp.add_argument(
        "--unsafe-synced-fs",
        action="store_true",
        help="override D2 hard refusal when a watch/ledger sits on cloud-sync or fails "
             "hard-link probe (logged; data-loss risk — FACTORY-PLAN D2)",
    )
    sp.set_defaults(func=_cmd_factory)

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
