"""The walker — the execution engine (ARCHITECTURE §3).

``walk(plan, run_dir, …)`` consumes a :class:`~cairn.kernel.plan.Plan` against a run dir,
one node at a time, deriving *all* progress from disk: a step is done iff its ``produces``
exist and validate, a gate is answered iff ``gates/<name>.json`` exists, a loop's cycle is
``1 + the count of fully-valid prior cycles``. So **resume is just walk() on an existing
run dir** — every done node skips, the first not-done node re-executes; no counters are
stored anywhere but the artifacts and the trail.

**Resume done-ness precedence: recorded decision > artifact predicate > re-run.** A node's
recorded ``run.json`` status can outrank what is on disk: a ``skipped`` node stays skipped
(a self-skip is a completed decision — re-firing it would burn an invocation every resume),
a ``halted`` node re-runs unconditionally (a blocked step that wrote a valid stub must not
silently pass), and every other status falls through to the artifact predicate (so a
crash-mid-step with valid outputs still skips). See :meth:`_Walk._is_done`.

The four node shapes (ARCHITECTURE §3.1-3.6) — steps come in three actors (machine
``run``, model ``agent``, human ``manual``) sharing one needs/produces contract:

- **step** — done-skip → needs-check → compose/render → invoke → validate ``produces``
  (the authority over the STEP block, §7) → retry-with-feedback or halt. ``agent`` steps
  go through the injected composer + their resolved executor; ``run`` steps render the
  command leniently (§2.8) and go to the shell executor; ``manual`` steps prompt an
  operator (headless ⇒ halt exit 6).
- **gate** — delegated to :mod:`cairn.kernel.gatekit`, resumable via its decision file.
- **parallel** — a ``ThreadPoolExecutor``; the trail stays single-writer via one lock.
- **loop** — a bounded review⇄revise loop, cycle recomputed from disk each entry.

Every failure is a typed halt: a trail ``run-halt`` with ``{node, reason,
validator_reasons, exit_code}``, partial artifacts left in place, and the process exit
code from the §9 taxonomy. The whole walk holds the run's advisory lock so two resumes
can never interleave.

Out of scope here (TODO): budgets, guard shims.
Stdlib + pinned kernel modules only.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import cairn
from cairn.executors.base import ExecTimeout
from cairn.kernel import durafs
from cairn.kernel.agent_slots import (
    DEFAULT_POLL_S,
    release_slot,
    refresh_slot,
    slots_dir_for,
    wait_acquire_slot,
)
from cairn.kernel.artifacts import (
    DEFAULT_VALIDATOR_TIMEOUT_S,
    done,
    exists,
    resolve_path,
    validate,
)
from cairn.kernel.compose import render_artifact_path
from cairn.kernel.config import Config
from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.expr import EvalError, Expr
from cairn.kernel.gatekeys import ensure_run_key, guard_manifest_path
from cairn.kernel.gatekit import (
    GateNeedsHuman,
    GateTampered,
    GateUnanswered,
    read_verified_choice,
    read_verified_decision,
    resolve_gate,
)
from cairn.kernel.guards import write_manifest
from cairn.kernel.hookprobe import _looks_like_auth_failure
from cairn.kernel.plan import (
    GateNode,
    GuardDecl,
    LoopNode,
    ParallelNode,
    Plan,
    StepNode,
    render_run_id,
)
from cairn.kernel.runstate import (
    LockHeldError,
    RunExistsError,
    create_run,
    load_run,
    node_status,
    run_lock,
    set_node_status,
    update_run,
)
from cairn.kernel.schemas import get_schema
from cairn.kernel.sinks import build_tee_sinks
from cairn.kernel.template import HELPERS, TemplateContext, TemplateError, render
from cairn.kernel.trail import format_at, make_redactor
from cairn.kernel.types import ExitCode, Invocation

# The template placeholder + helper shapes, mirrored here so lenient command rendering can
# decide per-``{…}`` whether a brace is cairn's (substitute) or foreign (pass through).
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_HELPER_CALL = re.compile(r"(\w+)\((.*)\)")
_VALUE_KEYWORDS = frozenset({"pipeline", "date", "datetime", "cycle"})

# A real auth/environment failure is terminal (BLOCKED/9, not EXECUTOR/4): the vendor CLI
# prints the error and exits, so the signal sits in the last handful of lines, not mid-
# transcript. Classifying from the whole log would false-positive on a legitimate (possibly
# long) coding step whose output happens to contain a broad sign ("please run",
# "authentication", "/login", "log in to") — or whose task IS auth/login work — and costs an
# unbounded read on a large agent log. 8 KiB comfortably covers a vendor CLI's closing error
# block. Spawn/crash failures without this signature stay EXECUTOR(4).
_AUTH_TAIL_BYTES = 8192


def _tail_text(path: Path, max_bytes: int) -> str:
    """The last ``max_bytes`` of ``path``, decoded leniently. Empty string if absent."""
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - max_bytes))
        return f.read().decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# cursor: primitive helpers (TRIGGERS-PLAN.md §4) — module-level, no `self` needed.
# --------------------------------------------------------------------------- #


@contextmanager
def _cursor_lock(cursor_path: Path) -> Iterator[None]:
    """flock on a companion ``<cursor>.lock`` file — mirrors ``runstate.run_lock`` (a
    DEDICATED lock file, never the state file itself, so acquiring the lock never
    touches/truncates already-committed content) — but BLOCKING (``LOCK_EX``, no
    ``LOCK_NB``): two concurrently *scheduled* polls (§4) must serialize their commit
    and both eventually succeed, not have the loser error out the way a second
    ``cairn resume`` does against a run's advisory lock.
    """
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = cursor_path.with_name(cursor_path.name + ".lock")
    lock_path.touch(exist_ok=True)
    fh = lock_path.open("r+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _atomic_write_cursor(path: Path, doc: dict) -> None:
    """Durably replace the cursor file via :func:`cairn.kernel.durafs.atomic_write_text`.

    The cursor is a state authority (like ``run.json``): an un-fsynced tmp lost to a
    crash mid-rename would silently re-widen the next poll's window, defeating the
    entire point of a persisted watermark. Formatting stays byte-identical to the
    historical hand-rolled write (``indent=2, ensure_ascii=False``).
    """
    durafs.atomic_write_text(path, json.dumps(doc, indent=2, ensure_ascii=False))


def _assert_cursor_contained(step_id: str, cursor_rel: str, candidate: Path, real_workspace_dir: Path) -> None:
    """Resolve ``candidate`` (following symlinks) and require it to stay under
    ``real_workspace_dir`` — mirrors ``artifacts._assert_contained`` (codex-F11), applied to
    the workspace boundary instead of the run-dir boundary.

    ``plan._parse_cursor`` already rejects an absolute/``..``-containing ``cursor:`` path at
    PLAN time, but that is a lexical check only: a run-local-looking path can still point,
    via a symlink anywhere in its chain, outside the workspace — and the workspace is the
    one place a run deliberately reads/writes OUTSIDE its own run dir (§4's cross-run
    watermark), so it is MORE exposed to this than a run dir is, not less (any earlier run's
    step, or anything else with workspace write access, could have planted the symlink).
    Checked at runtime, on every resolution, the same way ``resolve_path`` re-checks every
    artifact candidate rather than trusting the plan-time parse alone.

    ``resolve()`` is strict=False: a not-yet-committed cursor file still resolves fine as
    long as its parent chain has no escaping symlink, so this never rejects the ordinary
    "first commit" path.

    Known residual (TOCTOU, disclosed not closed — same shape as ``artifacts._assert_contained``'s,
    SECURITY.md §1.4): callers read/write through the UNRESOLVED ``self.workspace_dir / cursor``
    after this check passes, so a symlink swapped in during the narrow same-process window
    between this check and that read/write could still redirect it; accepted as a documented
    residual under the same "requires prior workspace write access" threat model.
    """
    resolved = candidate.resolve()
    if resolved != real_workspace_dir and real_workspace_dir not in resolved.parents:
        raise ConfigError(
            f"step {step_id!r}: cursor path escapes the workspace via symlink: {cursor_rel}"
        )


# --------------------------------------------------------------------------- #
# Internal control flow.
# --------------------------------------------------------------------------- #


class _Halt(Exception):
    """A node failed — carries everything the ``run-halt`` event and exit code need."""

    def __init__(
        self,
        exit_code: ExitCode,
        node: str | None,
        reason: str,
        validator_reasons: list[str] | None = None,
    ) -> None:
        super().__init__(reason)
        self.exit_code = exit_code
        self.node = node
        self.reason = reason
        self.validator_reasons = validator_reasons or []


# --------------------------------------------------------------------------- #
# bootstrap_run — mint a fresh run dir + validated run.json.
# --------------------------------------------------------------------------- #


def bootstrap_run(
    workspace_dir: Path,
    plan: Plan,
    *,
    now: datetime,
    runs_root: Path | None = None,
    run_dir: Path | None = None,
    pipeline_hash: str = "sha256:unknown",
) -> Path:
    """Create the run dir and write its validated ``run.json``; return the dir.

    The run id is ``render_run_id(plan, now)``; a name collision auto-suffixes ``-v2``,
    ``-v3``… (the escape hatch ``run_dir=`` names an exact dir and does *not* retry). The
    manifest conforms to the packaged ``cairn:run`` schema: params/dims/executors/models
    all recorded so a resume re-plans against the same inputs and ``cairn ps`` reads true.
    """
    workspace_dir = Path(workspace_dir)
    models = {
        step_id: (f"{model}/{effort}" if effort else model)
        for step_id, (_exec, model, effort) in plan.resolved_models.items()
    }

    def payload(run_id: str) -> dict:
        return {
            "run_id": run_id,
            "pipeline": plan.pipeline,
            "pipeline_hash": pipeline_hash,
            "cairn_version": cairn.__version__,
            "params": plan.params,
            "dims": plan.dims,
            "executors": {
                "default": plan.executor_default or "",
                "overrides": {},
                "versions": {},
            },
            "models": models,
            "created_at": format_at(now),
            "status": "running",
            "nodes": {},
        }

    if run_dir is not None:
        run_dir = Path(run_dir)
        created = create_run(run_dir.parent, run_dir.name, payload(run_dir.name))
        ensure_run_key(created)  # mint the gate-decision secret before any gate commits
        return created

    root = Path(runs_root) if runs_root is not None else workspace_dir / "runs"
    base = render_run_id(plan, now)
    run_id = base
    suffix = 1
    while True:
        try:
            created = create_run(root, run_id, payload(run_id))
        except RunExistsError:
            suffix += 1
            run_id = f"{base}-v{suffix}"
            continue
        ensure_run_key(created)  # mint the gate-decision secret before any gate commits
        return created


def invalidate_from(plan: Plan, run_dir: Path, from_node: str, *, now: datetime) -> tuple[int, int]:
    """``cairn resume --from <node>``: force every node from ``from_node`` onward (walk
    order) to re-execute on the next walk, even though its artifacts still validate.

    The walker's done-predicates are artifact-driven (:meth:`_Walk._is_done`,
    :meth:`_Walk._completed_cycles`), so clearing recorded statuses alone re-runs nothing —
    a stale-but-valid artifact immediately re-satisfies the predicate. That is the exact
    failure this exists to fix: a step's code was corrected *after* the step ran, and
    skip-if-done kept serving the artifact built by the buggy code. Invalidation therefore
    does both halves:

    - every existing artifact the affected nodes produce (all loop cycles; gate decision
      files too — a decision made on evidence about to be regenerated must be re-confirmed)
      moves into ``superseded/<stamp>/`` preserving relative paths: the proof is withdrawn,
      never destroyed;
    - the affected node records leave run.json (a recorded ``skipped``/``halted`` decision
      must not outrank an explicit re-execution request) and the run status returns to
      ``running``.

    Returns ``(node records cleared, artifact files moved)``. An unknown node id raises
    ConfigError naming the valid ids, mirroring the plan-range ``--from``.
    """
    run_dir = Path(run_dir)
    ids = [n.id if isinstance(n, StepNode) else n.name for n in plan.nodes]
    if from_node not in ids:
        raise ConfigError(f"--from names unknown node {from_node!r} (nodes: {ids})")

    node_ids: list[str] = []
    to_move: dict[Path, None] = {}  # insertion-ordered set — cycle-invariant paths render identically per cycle

    def step_files(step: StepNode, cycle: int | None) -> None:
        for name in step.produces:
            decl = plan.artifacts.get(name)
            if decl is None:
                continue
            rendered = render_artifact_path(
                decl, params=plan.params, dims=plan.dims,
                pipeline=plan.pipeline, cycle=cycle, now=now,
            )
            for path in resolve_path(decl, rendered, run_dir).paths:
                if path.is_file():
                    to_move[path] = None

    def collect(node: Any, cycles: list[int | None]) -> None:
        if isinstance(node, StepNode):
            node_ids.append(node.id)
            for c in cycles:
                step_files(node, c)
        elif isinstance(node, GateNode):
            node_ids.append(node.name)
            decision = run_dir / "gates" / f"{node.name}.json"
            if decision.is_file():
                to_move[decision] = None
        elif isinstance(node, ParallelNode):
            node_ids.append(node.name)
            for child in node.steps:
                collect(child, cycles)
        elif isinstance(node, LoopNode):
            node_ids.append(node.name)
            cap = max(node.max_interactive, node.max_headless)
            for child in node.body:
                collect(child, list(range(1, cap + 1)))

    for node in plan.nodes[ids.index(from_node):]:
        collect(node, [None])

    dest_root = run_dir / "superseded" / now.strftime("%Y%m%dT%H%M%SZ")
    moved = 0
    with run_lock(run_dir):
        for path in to_move:
            dest = dest_root / path.relative_to(run_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            path.rename(dest)
            moved += 1

        cleared = 0

        def mutate(doc: dict) -> None:
            nonlocal cleared
            nodes = doc.get("nodes") or {}
            for nid in node_ids:
                if nodes.pop(nid, None) is not None:
                    cleared += 1
            doc["nodes"] = nodes
            doc["status"] = "running"

        update_run(run_dir, mutate)
    return cleared, moved


# --------------------------------------------------------------------------- #
# walk — the public entry point.
# --------------------------------------------------------------------------- #


def walk(
    plan: Plan,
    run_dir: Path,
    *,
    workspace_dir: Path,
    config: Config,
    executors: dict[str, Any],
    composer: Callable[..., str],
    interactive: bool,
    gate_presets: dict[str, str],
    now: datetime,
    validator_timeout_s: int | None = None,
    gate_preset_by: dict[str, str] | None = None,
) -> ExitCode:
    """Walk ``plan`` against ``run_dir`` to completion or the first halt.

    Idempotent per node, so calling it again on the same run dir *is* resume. The whole
    walk holds the run's advisory lock; a concurrent holder makes this return
    ``ExitCode.EXECUTOR`` with a stderr message rather than interleaving.

    ``gate_preset_by`` threads ledger provenance for presets (``"flag"`` vs
    ``"lane:<name>"``); absent → every preset records ``by: "flag"`` (today's shape).
    """
    run_dir = Path(run_dir)
    try:
        with run_lock(run_dir):
            return _Walk(
                plan=plan,
                run_dir=run_dir,
                workspace_dir=Path(workspace_dir),
                config=config,
                executors=executors,
                composer=composer,
                interactive=interactive,
                gate_presets=gate_presets,
                gate_preset_by=dict(gate_preset_by or {}),
                now=now,
                timeout=validator_timeout_s
                if validator_timeout_s is not None
                else DEFAULT_VALIDATOR_TIMEOUT_S,
            ).run()
    except LockHeldError as exc:
        print(f"cairn: {exc}", file=sys.stderr)
        return ExitCode.EXECUTOR


@dataclass
class _Walk:
    plan: Plan
    run_dir: Path
    workspace_dir: Path
    config: Config
    executors: dict[str, Any]
    composer: Callable[..., str]
    interactive: bool
    gate_presets: dict[str, str]
    now: datetime
    timeout: int
    gate_preset_by: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # One RLock serializes every trail append AND every run.json mutation, so N parallel
        # children keep the trail single-writer and the manifest consistent (OBSERVABILITY §1).
        self._lock = threading.RLock()
        self._trail = None  # set in run()
        self._redactor: Callable[[str], str] | None = None  # built in run() from [secrets]
        self._schema_path = self.run_dir / ".cairn" / "step-return.json"
        self._gate_nodes: dict[str, GateNode] | None = None  # name→GateNode, built lazily

    # -- top-level orchestration -------------------------------------------- #

    def run(self) -> ExitCode:
        from cairn.kernel.trail import TrailWriter

        run_doc = load_run(self.run_dir)
        run_id = run_doc["run_id"]
        self._write_step_return_schema()

        # Redaction (SECURITY §1.3) + sinks (OBSERVABILITY §2) are wired once, here, and shared:
        # the same value-scrubber goes into the trail (every event line) and into each step's
        # Invocation (the executor's log-write path). Tee sinks push each redacted event.
        self._redactor = make_redactor(self._secret_values())
        tee_sinks = build_tee_sinks(self.config.sinks)
        try:
            trail_writer = TrailWriter(
                self.run_dir, run_id, redactor=self._redactor, tee_sinks=tee_sinks
            )
        except BaseException:
            # TrailWriter owns closing the tees once constructed; if construction itself
            # fails, close them here so no webhook daemon thread outlives the walk.
            for sink in tee_sinks:
                try:
                    sink.close()
                except Exception:  # noqa: BLE001 — best-effort cleanup on the failure path
                    pass
            raise

        with trail_writer as trail:
            self._trail = trail
            # The whole run body — run-start, guard install, the plan emit, the node loop,
            # AND the success tail — lives inside this one try. Anything that can raise
            # before the first node even dispatches (e.g. a plugin's install_guards) must
            # hit the same belt as a mid-walk failure: a bare `except BaseException` scoped
            # only around the node loop would let those escape with run.json stuck
            # "running" — the exact corpse the belt exists to prevent (claude-F4).
            try:
                self._emit(
                    "run-start",
                    data={
                        "params": self.plan.params,
                        "dims": self.plan.dims,
                        "executors": {"default": self.plan.executor_default or ""},
                    },
                )
                self._install_guards()
                self._emit(
                    "plan",
                    data={
                        "pipeline_hash": run_doc.get("pipeline_hash"),
                        "nodes": [self._node_id(n) for n in self.plan.nodes],
                        "models": {
                            sid: (f"{m}/{e}" if e else m)
                            for sid, (_x, m, e) in self.plan.resolved_models.items()
                        },
                    },
                )
                for node in self.plan.nodes:
                    self._dispatch(node, None)
                self._emit("run-done", data={"nodes": len(self.plan.nodes)})
                self._mark_run("done")
                return ExitCode.OK
            except _Halt as halt:
                if halt.node is not None:
                    # A needs-human halt (manual/gate awaiting an operator) is not a node
                    # failure — leave it "running" so a resume, once the operator fulfils
                    # the produce/answers the gate, satisfies it via the artifact predicate
                    # instead of force-re-running it (which "halted" would mandate, §3.5).
                    node_stat = "running" if halt.exit_code == ExitCode.NEEDS_HUMAN else "halted"
                    self._set_status(halt.node, node_stat)
                self._emit(
                    "run-halt",
                    node=halt.node,
                    data={
                        "reason": halt.reason,
                        "validator_reasons": halt.validator_reasons,
                        "exit_code": int(halt.exit_code),
                    },
                )
                self._mark_run("halted")
                return halt.exit_code
            except BaseException as exc:
                # Any escape that is not a clean _Halt (an unguarded stdlib exception, a
                # kernel bug) must still leave the run in a terminal, non-"running" state —
                # gc (SECURITY §5) never collects a run stuck "running" (claude-F4 belt).
                # Record the halt, then re-raise unchanged so the CLI still exits non-zero;
                # this never swallows the exception.
                self._emit(
                    "run-halt",
                    node=None,
                    data={
                        "reason": f"internal-error: {exc}",
                        "validator_reasons": [],
                        "exit_code": int(ExitCode.EXECUTOR),
                    },
                )
                self._mark_run("halted")
                raise

    def _install_guards(self) -> None:
        seen: set[int] = set()
        for ex in self.executors.values():
            if id(ex) in seen:
                continue
            seen.add(id(ex))
            ex.install_guards(self.plan.guards, self.workspace_dir, self.run_dir)

    def _dispatch(self, node: Any, cycle: int | None) -> None:
        if isinstance(node, StepNode):
            self._run_step(node, cycle)
        elif isinstance(node, GateNode):
            self._run_gate(node, cycle)
        elif isinstance(node, ParallelNode):
            self._run_parallel(node)
        elif isinstance(node, LoopNode):
            self._run_loop(node)
        else:  # pragma: no cover - Node is a closed union
            raise _Halt(ExitCode.CONFIG, self._node_id(node), f"unknown node kind {node!r}")

    # -- step --------------------------------------------------------------- #

    def _run_step(self, step: StepNode, cycle: int | None) -> None:
        if not self._passes_when(step.when_runtime, cycle, step.id):
            self._emit("step-skip", node=step.id, cycle=cycle, data={"reason": "when: false"})
            self._set_status(step.id, "skipped")
            return

        # Done-skip first, for every kind — so a resumed run skips an already-satisfied
        # manual step (produces valid) rather than re-halting for an operator.
        if self._is_done(step, cycle):
            self._set_status(step.id, "done")
            return

        if step.kind == "manual":
            self._run_manual(step, cycle)
            return

        self._check_needs(step, cycle)
        self._execute_step(step, cycle)

    def _execute_step(self, step: StepNode, cycle: int | None) -> None:
        if step.kind == "run":
            executor = self.executors.get("shell")
            if executor is None:
                raise _Halt(ExitCode.EXECUTOR, step.id, "no 'shell' executor configured")
            model, effort = "shell", None

            def make_prompt(_attempt: int, _reasons: list[str]) -> str:
                return self._render_command(step.command or "", cycle, step)
        else:  # agent
            exec_name, model, effort = self.plan.resolved_models[step.id]
            executor = self.executors.get(exec_name)
            if executor is None:
                raise _Halt(ExitCode.EXECUTOR, step.id, f"no executor {exec_name!r} configured")

            def make_prompt(_attempt: int, reasons: list[str]) -> str:
                return self.composer(
                    step=step,
                    plan=self.plan,
                    run_dir=self.run_dir,
                    cycle=cycle,
                    retry_reasons=reasons,
                )

        env = self._build_env(step)
        env.update(self._active_guard_manifest(step, cycle))
        retry_attempts, feedback = step.retry
        model_str = f"{model}/{effort}" if effort else model
        reasons: list[str] = []
        attempt = 1

        while True:
            prompt_path = self._log_path(step.id, "prompt.md", attempt, cycle)
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(make_prompt(attempt, reasons), encoding="utf-8")
            log_path = self._log_path(step.id, "log", attempt, cycle)

            self._emit(
                "step-start",
                node=step.id,
                attempt=attempt,
                cycle=cycle,
                data={"model": model_str, "log_path": self._rel(log_path)},
            )
            inv = Invocation(
                prompt_file=prompt_path,
                model=model,
                effort=effort,
                cwd=self.run_dir,
                env=env,
                timeout_s=step.timeout_s,
                log_path=log_path,
                return_schema=self._schema_path,
                network=step.network,
                redactor=self._redactor,
            )
            # Agent-slot pool (W6): hold a numbered O_EXCL slot to spawn an agent
            # step. Wait is BEFORE invoke so step.timeout_s is untouched. run:/
            # manual: never acquire. Absent [factory] max_agents ⇒ OFF (D7).
            slot_name: str | None = None
            if step.kind != "run":
                slot_name = self._acquire_agent_slot(step, attempt=attempt, cycle=cycle)
            try:
                with self._heartbeat(
                    step.id, attempt, cycle, log_path, slot_name=slot_name
                ):
                    result = executor.invoke(inv)
            except ExecTimeout as exc:
                self._emit("timeout", node=step.id, attempt=attempt, cycle=cycle, data={"error": str(exc)})
                raise _Halt(ExitCode.TIMEOUT, step.id, f"timeout: {exc}") from exc
            except CairnError as exc:
                self._emit("step-fail", node=step.id, attempt=attempt, cycle=cycle, data={"error": str(exc)})
                raise _Halt(ExitCode.EXECUTOR, step.id, f"executor failure: {exc}") from exc
            finally:
                if slot_name is not None:
                    release_slot(slots_dir_for(self.workspace_dir), slot_name)

            block = result.step or {}
            # Defensive belt: parse_step_sentinel schema-validates learnings before returning a
            # block (executors/base.py), but Result.step is a plain dict any Executor can hand
            # back directly (bypassing that gate) — guard so a non-object member never raises
            # AttributeError deep in the walker (codex-F10).
            for learn in block.get("learnings") or []:
                if not isinstance(learn, dict):
                    continue
                self._emit("learn", node=step.id, cycle=cycle, data={k: learn.get(k) for k in ("note", "tag")})

            status = block.get("status")
            if status == "skipped" and step.skippable:
                self._emit("step-skip", node=step.id, attempt=attempt, cycle=cycle,
                           data={"reason": block.get("summary", "")})
                self._set_status(step.id, "skipped")
                return
            if status == "blocked":
                blockers = block.get("blockers") or []
                self._emit("step-fail", node=step.id, attempt=attempt, cycle=cycle, data={"blockers": blockers})
                raise _Halt(ExitCode.GATE_FAILED, step.id, "step returned blocked", blockers)

            ok, validator_reasons = self._validate_produces(step, cycle)
            if result.exit_code != 0:
                # A non-zero exit is checked for every step kind now (codex-F7/grok-F11/
                # claude-F3) — an agent CLI that fails auth/API but happens to leave a
                # valid-looking artifact must not be recorded step-done. Split by shape:
                # an auth/environment signature in the output TAIL is BLOCKED(9), not a
                # content problem and not a hard executor crash — retrying it just
                # re-invokes a CLI that cannot succeed, so halt immediately (waiting-class
                # park) and skip whatever retry budget remains. Spawn/crash without this
                # signature stay EXECUTOR(4). Everything else is a genuine content failure
                # and keeps the existing retry-with-feedback path.
                tail_text = _tail_text(log_path, _AUTH_TAIL_BYTES)
                if _looks_like_auth_failure(tail_text, result.exit_code):
                    self._emit(
                        "step-fail",
                        node=step.id,
                        attempt=attempt,
                        cycle=cycle,
                        data={"error": f"command exited with code {result.exit_code} (auth/environment failure)"},
                    )
                    raise _Halt(
                        ExitCode.BLOCKED,
                        step.id,
                        f"blocked: command exited with code {result.exit_code} (auth/environment)",
                    )
                ok = False
                validator_reasons = validator_reasons + [f"command exited with code {result.exit_code}"]

            if ok:
                # `ok` here means _validate_produces passed AND exit_code == 0 (a nonzero
                # exit forced ok=False above unless it already _Halt'd on the auth path) —
                # exactly the "step succeeded" gate the cursor commit must wait for (§4: a
                # failed/halted poll must re-fetch, never silently advance the watermark).
                cursor_warning = self._commit_cursor(step, cycle) if step.cursor else None
                data: dict[str, Any] = {
                    "artifacts": self._produced_paths(step, cycle),
                    "duration_s": result.duration_s,
                }
                if block.get("metrics"):
                    data["metrics"] = block["metrics"]
                # Executor-reported usage (json output-format, future) outranks a model's
                # self-reported STEP-block usage; today result.usage is None → block wins.
                # An executor-reported {} still wins the precedence (the block's self-report
                # must not leak through it), but carries no numbers — the truthiness guard
                # then omits it from the event, keeping step-done lean.
                usage = result.usage if result.usage is not None else block.get("usage")
                if usage:
                    data["usage"] = usage
                if cursor_warning:  # I2 — folded into step-done, not a new trail event kind
                    data["cursor_warning"] = cursor_warning
                self._emit("step-done", node=step.id, attempt=attempt, cycle=cycle, data=data)
                self._set_status(step.id, "done", cycles=cycle)
                return

            if attempt <= retry_attempts:
                self._emit("retry", node=step.id, attempt=attempt, cycle=cycle,
                           data={"validator_reasons": validator_reasons})
                reasons = validator_reasons if feedback else []
                attempt += 1
                continue

            self._emit("step-fail", node=step.id, attempt=attempt, cycle=cycle,
                       data={"validator_reasons": validator_reasons})
            raise _Halt(ExitCode.GATE_FAILED, step.id, "artifact validation failed", validator_reasons)

    def _run_manual(self, step: StepNode, cycle: int | None) -> None:
        rendered = self._render_command(step.command or "", cycle, step)
        prompt_path = self._log_path(step.id, "prompt.md", 1, cycle)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(rendered, encoding="utf-8")

        if not self.interactive:
            self._emit("gate-pending", node=step.id, cycle=cycle,
                       data={"manual": rendered, "produces": list(step.produces)})
            raise _Halt(ExitCode.NEEDS_HUMAN, step.id, "manual step requires an operator")

        print(rendered)
        print("Produces:", ", ".join(step.produces) or "(none)")
        try:
            input("Press Enter when done... ")
        except (EOFError, KeyboardInterrupt) as exc:
            # A closed/interrupted TTY is not an unhandled crash: it is the operator
            # declining to answer → the same typed needs-human halt as headless.
            self._emit("gate-pending", node=step.id, cycle=cycle,
                       data={"manual": rendered, "produces": list(step.produces)})
            raise _Halt(ExitCode.NEEDS_HUMAN, step.id, "manual step interrupted (no operator)") from exc
        ok, reasons = self._validate_produces(step, cycle)
        if not ok:
            self._emit("step-fail", node=step.id, cycle=cycle, data={"validator_reasons": reasons})
            raise _Halt(ExitCode.GATE_FAILED, step.id, "manual step produces invalid", reasons)
        self._emit("step-done", node=step.id, cycle=cycle, data={"artifacts": self._produced_paths(step, cycle)})
        self._set_status(step.id, "done")

    # -- gate --------------------------------------------------------------- #

    def _run_gate(self, gate: GateNode, cycle: int | None) -> None:
        if not self._passes_when(gate.when_runtime, cycle, gate.name):
            self._emit("step-skip", node=gate.name, cycle=cycle, data={"reason": "when: false"})
            self._set_status(gate.name, "skipped")
            return
        try:
            # The REAL transition time (like _set_status, claude-F12's fix), not self.now: the
            # `now` passed here only ever feeds gatekit._commit's `at` stamp on the decision
            # file (gates/<name>.json, signed into the W2 HMAC as-written and verified
            # as-written — no path/render/determinism use), so there's no determinism
            # requirement pulling it back to the frozen construction-time clock.
            resolve_gate(
                gate,
                self.run_dir,
                interactive=self.interactive,
                presets=self.gate_presets,
                preset_by=self.gate_preset_by,
                emit=self._emit,
                now=datetime.now(timezone.utc),
            )
        except GateNeedsHuman as exc:
            raise _Halt(ExitCode.NEEDS_HUMAN, gate.name, "gate needs a human decision") from exc
        except CairnError as exc:
            raise _Halt(ExitCode.CONFIG, gate.name, str(exc)) from exc
        self._set_status(gate.name, "done")

    # -- parallel ----------------------------------------------------------- #

    def _run_parallel(self, node: ParallelNode) -> None:
        if not self._passes_when(node.when_runtime, None, node.name):
            self._emit("step-skip", node=node.name, data={"reason": "when: false"})
            self._set_status(node.name, "skipped")
            return

        self._set_status(node.name, "running")
        halts: list[_Halt] = []
        with ThreadPoolExecutor(max_workers=len(node.steps)) as pool:
            futures = {pool.submit(self._run_step, child, None): child for child in node.steps}
            for future in as_completed(futures):
                try:
                    future.result()
                except _Halt as halt:
                    halts.append(halt)
                    if node.on_fail == "fast":
                        # Best-effort: cancel siblings that haven't started; running ones finish.
                        pool.shutdown(wait=False, cancel_futures=True)
                        break

        if halts:
            self._set_status(node.name, "halted")
            raise halts[0]
        self._set_status(node.name, "done")

    # -- loop --------------------------------------------------------------- #

    def _run_loop(self, node: LoopNode) -> None:
        if not self._passes_when(node.when_runtime, None, node.name):
            self._emit("step-skip", node=node.name, data={"reason": "when: false"})
            self._set_status(node.name, "skipped")
            return

        completed = self._completed_cycles(node)
        # Resume shortcut: a loop whose until: already held at the last valid cycle is done.
        if node.until is not None and completed >= max(node.min, 1):
            if self._eval(node.until, completed, node.name):
                self._set_status(node.name, "done", cycles=completed)
                return

        cap = node.max_interactive if self.interactive else node.max_headless
        cycle = completed + 1
        broke = False
        while cycle <= cap:
            self._emit("cycle-start", node=node.name, cycle=cycle, data={})
            for child in node.body:
                self._dispatch(child, cycle)
            if cycle >= node.min and node.until is not None and self._eval(node.until, cycle, node.name):
                broke = True
                completed = cycle
                break
            completed = cycle
            cycle += 1

        if not broke and node.until is not None:
            # Cap reached without the exit condition — the on_cap policy decides.
            self._emit("loop-capped", node=node.name, cycle=completed, data={"until": node.until.source})
            if node.on_cap == "halt":
                raise _Halt(ExitCode.GATE_FAILED, node.name, "loop hit its cap without satisfying until")

        self._set_status(node.name, "done", cycles=completed)

    def _completed_cycles(self, node: LoopNode) -> int:
        """Largest N with cycles 1..N ALL having every body ``produces`` valid (re-validated).

        Bounded by the loop's own cap (codex-F3 runtime backstop): a cycle-invariant produce
        (one whose path template omits ``{cycle}``) renders byte-identically every cycle, so
        it would otherwise never fail validation and this loop would spawn validators forever.
        cairn.kernel.plan rejects that shape at plan time (a new loop-body produce must
        reference ``{cycle}``); this cap is the backstop for a plan that reaches the walker
        without going through that check (e.g. hand-built, or an older/bypassed plan).
        """
        decls = [
            (name, self.plan.artifacts[name])
            for child in node.body
            if isinstance(child, StepNode)
            for name in child.produces
            if name in self.plan.artifacts
        ]
        if not decls:
            return 0
        cap = node.max_interactive if self.interactive else node.max_headless
        n = 0
        while n < cap:
            k = n + 1
            for name, decl in decls:
                rendered = render_artifact_path(
                    decl, params=self.plan.params, dims=self.plan.dims,
                    pipeline=self.plan.pipeline, cycle=k, now=self.now,
                )
                resolved = resolve_path(decl, rendered, self.run_dir)
                if not validate(resolved, decl, self.run_dir, self.workspace_dir, self.timeout).ok:
                    return n
            n = k
        return n

    # -- artifact predicates ------------------------------------------------ #

    def _is_done(self, step: StepNode, cycle: int | None) -> bool:
        """Resume/skip predicate — recorded decision outranks the artifact predicate.

        Precedence (ARCHITECTURE §3.5): a recorded ``skipped`` is a *completed decision*
        (a self-skip must not re-fire and burn an invocation every resume) → done; a
        recorded ``halted`` (blocked or failed) is NOT done regardless of what is on disk
        (a blocked step that wrote a valid stub must not silently pass) → re-run; every
        other status (``done``/``running``/absent — the crash-mid-step case) falls through
        to the artifact predicate: every declared ``produces`` exists AND validates.
        """
        status = self._node_status(step.id)
        if status == "skipped":
            return True
        if status == "halted":
            return False

        declared = [n for n in step.produces if n in self.plan.artifacts]
        if not declared:
            return False  # nothing provable → always (re-)run
        ok, _ = self._validate_produces(step, cycle)
        return ok

    def _node_status(self, node_id: str) -> str | None:
        return node_status(load_run(self.run_dir), node_id)

    def _validate_produces(self, step: StepNode, cycle: int | None) -> tuple[bool, list[str]]:
        resolved = []
        decls = {}
        for name in step.produces:
            decl = self.plan.artifacts.get(name)
            if decl is None:
                continue  # undeclared produce — nothing to validate against
            rendered = render_artifact_path(
                decl, params=self.plan.params, dims=self.plan.dims,
                pipeline=self.plan.pipeline, cycle=cycle, now=self.now,
            )
            resolved.append(resolve_path(decl, rendered, self.run_dir))
            decls[name] = decl
        if not resolved:
            return True, []  # side-effect step / undeclared produces — nothing to check
        ok, results = done(resolved, decls, self.run_dir, self.workspace_dir, self.timeout)
        reasons = [r for res in results.values() for r in res.reasons]
        return ok, reasons

    def _produced_paths(self, step: StepNode, cycle: int | None) -> list[str]:
        out = []
        for name in step.produces:
            decl = self.plan.artifacts.get(name)
            if decl is None:
                continue
            out.append(render_artifact_path(
                decl, params=self.plan.params, dims=self.plan.dims,
                pipeline=self.plan.pipeline, cycle=cycle, now=self.now,
            ))
        return out

    def _check_needs(self, step: StepNode, cycle: int | None) -> None:
        for name in step.needs:
            if name in self.plan.artifacts:
                decl = self.plan.artifacts[name]
                rendered = render_artifact_path(
                    decl, params=self.plan.params, dims=self.plan.dims,
                    pipeline=self.plan.pipeline, cycle=cycle, now=self.now,
                )
                if not exists(resolve_path(decl, rendered, self.run_dir)):
                    raise _Halt(ExitCode.CONFIG, step.id, f"required input {name!r} is missing")
            else:  # a gate name
                gate = self._gate_node(name)
                if gate is None:
                    raise _Halt(ExitCode.CONFIG, step.id, f"required gate {name!r} is unknown")
                # Verify, not just exist: a forged file must not satisfy a gate dependency.
                try:
                    read_verified_decision(self.run_dir, gate, self._emit)
                except (GateUnanswered, GateTampered) as exc:
                    raise _Halt(
                        ExitCode.CONFIG, step.id, f"required gate {name!r} is unanswered"
                    ) from exc

    # -- environment (SECURITY §1.2 — deny by default) ---------------------- #

    def _build_env(self, step: StepNode) -> dict[str, str]:
        env: dict[str, str] = {}
        # USER/LOGNAME are identity, not secrets: a macOS Keychain lookup (how `claude`/`codex`
        # find their stored OAuth credential) needs USER, so stripping it makes every executor
        # report "Not logged in". Kept in the deny-by-default baseline for that reason.
        # XDG_STATE_HOME must pass through so a guard-check subprocess (the shim or the claude
        # PreToolUse hook) resolves the SAME gatekeys/guard-manifest dir the parent minted+signed
        # under — without it, a run with XDG_STATE_HOME set can't load the per-run secret and every
        # guarded command fails closed (an availability break). It points at the state dir, not at
        # a secret; the key stays 0600 outside the run dir (SECURITY §6 — the sandbox is the guard,
        # not path secrecy).
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME", "XDG_STATE_HOME"):
            if key in os.environ:
                env[key] = os.environ[key]
        env["CAIRN_RUN_DIR"] = str(self.run_dir)
        env["CAIRN_STEP"] = step.id
        env["CAIRN_WORKSPACE"] = str(self.workspace_dir)
        env["CLAUDE_PROJECT_DIR"] = str(self.workspace_dir)

        dotenv = None
        for name in step.env:
            value = os.environ.get(name)
            if value is None:
                if dotenv is None:
                    dotenv = self._load_dotenv()
                value = dotenv.get(name)
            if value is None:
                raise _Halt(ExitCode.CONFIG, step.id, f"secret {name!r} is not set (env or workspace .env)")
            env[name] = value
        return env

    def _secret_values(self) -> dict[str, str]:
        """Resolve every declared ``[secrets]`` value for the run-wide redactor (SECURITY §1.3).

        Same source order as :meth:`_build_env` (process env → workspace ``.env``), but over the
        *declared* names rather than one step's ``env:`` — a secret must be scrubbed wherever it
        surfaces, not only in the steps that carry it. Only *resolved* values are returned (an
        undeclared/absent secret cannot leak, so there is nothing to scrub); a value is never
        logged anywhere, including on failure. Absent secrets are simply skipped here — the hard
        "secret required but unset" halt stays in :meth:`_build_env`, per step.
        """
        out: dict[str, str] = {}
        dotenv: dict[str, str] | None = None
        for name in self.config.secrets:
            value = os.environ.get(name)
            if value is None:
                if dotenv is None:
                    dotenv = self._load_dotenv()
                value = dotenv.get(name)
            if value:
                out[name] = value
        return out

    def _load_dotenv(self) -> dict[str, str]:
        path = self.workspace_dir / ".env"
        out: dict[str, str] = {}
        if not path.is_file():
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export ") or line.startswith("export\t"):
                line = line[len("export"):].lstrip()
            key, _, val = line.partition("=")
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]  # strip matching surrounding quotes
            out[key.strip()] = val
        return out

    # -- lenient command rendering (API §2.8) ------------------------------- #

    def _render_command(self, command: str, cycle: int | None, step: StepNode) -> str:
        ctx = self._command_context(cycle, step)

        def substitute(match: re.Match) -> str:
            body = match.group(1).strip()
            if not self._is_cairn_placeholder(body):
                return match.group(0)  # foreign braces (jq/awk/python literal) — verbatim
            try:
                return render(match.group(0), ctx)
            except TemplateError as exc:
                raise _Halt(ExitCode.CONFIG, step.id, f"command placeholder {{{body}}}: {exc}") from exc

        return _PLACEHOLDER.sub(substitute, command)

    def _command_context(self, cycle: int | None, step: StepNode) -> TemplateContext:
        def artifact(name: str) -> str:
            decl = self.plan.artifacts.get(name)
            if decl is None:
                raise KeyError(name)
            rendered = render_artifact_path(
                decl, params=self.plan.params, dims=self.plan.dims,
                pipeline=self.plan.pipeline, cycle=cycle, now=self.now,
            )
            return str(self.run_dir / rendered)

        def gate(name: str) -> Any:
            gate_node = self._gate_node(name)
            if gate_node is None:
                raise KeyError(name)
            # A forged choice must never reach a rendered shell command. A tampered file is
            # treated as unavailable (KeyError → command placeholder error → CONFIG halt); the
            # accessor emits gate-tamper before we raise.
            try:
                return read_verified_choice(self.run_dir, gate_node, self._emit)
            except (GateUnanswered, GateTampered) as exc:
                raise KeyError(name) from exc

        # cursor.value/cursor.next only resolve on a step that actually declared `cursor:`
        # (plan.py._parse_cursor already restricts that to run: steps) — None elsewhere, so
        # template.py's dotted-root lookup raises TemplateError and this step's placeholder
        # error names it (via the _Halt above), never silently renders "".
        cursor = None
        if step.cursor:
            cursor = {
                "value": self._cursor_value(step.id, step.cursor),
                "next": str(self._cursor_scratch_path(step.id)),
            }

        return TemplateContext(
            params=self.plan.params,
            dims=self.plan.dims,
            pipeline=self.plan.pipeline,
            cycle=cycle,
            now=self.now,
            artifact=artifact,
            gate=gate,
            run_dir=str(self.run_dir),
            cursor=cursor,
        )

    @staticmethod
    def _is_cairn_placeholder(body: str) -> bool:
        helper = _HELPER_CALL.fullmatch(body)
        if helper and helper.group(1) in HELPERS:
            return True
        if body == "run_dir":
            return True
        if ":" in body:
            return body.split(":", 1)[0].strip() in ("artifact", "gate")
        if body in _VALUE_KEYWORDS:
            return True
        return body.startswith("params.") or body.startswith("dims.") or body.startswith("cursor.")

    # -- cursor: primitive (TRIGGERS-PLAN.md §4) ----------------------------- #

    def _cursor_scratch_path(self, step_id: str) -> Path:
        """Absolute per-step scratch path ``{cursor.next}`` renders to. Lives under the
        run's ``.cairn/`` (already created by ``_write_step_return_schema`` before any
        node dispatches), so a step's write here never needs its own mkdir."""
        return self.run_dir / ".cairn" / f"cursor-next-{step_id}"

    def _cursor_value(self, step_id: str, cursor: str) -> str:
        """The committed watermark for a ``cursor:`` step — ``""`` when the file is
        missing or unreadable (§4: the first-ever poll has no watermark yet; that must
        render as empty, never raise).

        The containment check runs FIRST and is NOT part of that leniency (I3): a symlink
        escape must never be swallowed into "no value" — that would silently substitute a
        foreign file's content into a rendered shell command. It is a typed CONFIG halt
        naming the step and path instead, mirroring ``artifacts.resolve_path``'s runtime
        symlink re-check (codex-F11).
        """
        path = self.workspace_dir / cursor
        try:
            _assert_cursor_contained(step_id, cursor, path, self.workspace_dir.resolve())
        except ConfigError as exc:
            raise _Halt(ExitCode.CONFIG, step_id, str(exc)) from exc
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return ""
        value = doc.get("value") if isinstance(doc, dict) else None
        return value if isinstance(value, str) else ""

    def _commit_cursor(self, step: StepNode, cycle: int | None) -> str | None:
        """Commit ``{cursor.next}``'s scratch write to the step's watermark file. Called
        only from ``_execute_step``'s ``if ok:`` branch — i.e. after ``_validate_produces``
        passed AND ``exit_code == 0`` — so a failed/halted/still-retrying step never
        advances the watermark (§4: a failed poll must re-fetch its window, not skip it).

        Returns a warning string when the scratch write existed but could not be trusted
        (I2 — see below); ``None`` otherwise. The caller folds a non-``None`` return into
        the SAME ``step-done`` event's data the successful step already emits.

        WHY (global constraint): this is the one deliberate exception to "a run writes
        only inside its own run dir" — ``step.cursor`` resolves under the WORKSPACE root
        (plan-time validated absolute/``..``-free by ``plan._parse_cursor``), because the
        watermark is cross-run state by design and would be meaningless scoped to one run.
        """
        # I3: containment is checked at ENTRY, before the scratch-existence early return —
        # a malicious/foreign symlink at `step.cursor` is a config problem independent of
        # whether this attempt happened to write a scratch candidate. Never a silent
        # read/write outside the workspace; always a typed CONFIG halt naming the step.
        cursor_path = self.workspace_dir / step.cursor
        try:
            _assert_cursor_contained(step.id, step.cursor, cursor_path, self.workspace_dir.resolve())
        except ConfigError as exc:
            raise _Halt(ExitCode.CONFIG, step.id, str(exc)) from exc

        scratch = self._cursor_scratch_path(step.id)
        if not scratch.is_file():
            return None  # no scratch write this attempt → no advance, no error

        # I2 — deliberate LOUD-vs-LENIENT split, decided here: the step already SUCCEEDED
        # (produces validated, exit 0) by the time this runs, so an unreadable scratch must
        # never crash the walk or retroactively fail the step — that would contradict the
        # commit-on-valid protocol's own "step succeeded" gate. But it also must not be
        # silently swallowed like a merely-empty scratch: non-UTF-8 content is a STEP-
        # AUTHORING bug (the step's own command wrote garbage to {cursor.next}), and an
        # operator watching the trail needs to see it. So: lenient on CONTROL FLOW (no
        # advance, no halt, no exception), loud on SIGNAL — folded into the existing
        # step-done event's data (no new trail event type; walk.py's module docstring
        # commits to "every failure is a typed halt" for actual failures, and this isn't
        # one, so reusing step-done over inventing a new event kind keeps the trail
        # vocabulary as specified, not silently expanded). Round 2 found that data-only
        # signal is functionally write-only, though: `_print_walk_result` only prints
        # "run complete" on OK, and `trail --watch` never renders event data — so the
        # operator-visible half is a stderr line in the codebase's established warning
        # voice (runctl's reproducibility advisories, "cairn: warning — ..."), the one
        # channel that reaches both an interactive terminal and a headless scheduler's
        # captured logs. The trail's `cursor_warning` field stays too, for programmatic
        # `cairn trail` consumers — this adds the human-facing half, it doesn't replace it.
        try:
            candidate = scratch.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            reason = f"unreadable ({exc.__class__.__name__})"
            print(
                f"cairn: warning — step {step.id!r}: cursor scratch {reason}; cursor not advanced",
                file=sys.stderr,
            )
            return f"cursor scratch {reason} — watermark not advanced"
        if not candidate:
            return None  # empty scratch → no advance, no error

        with _cursor_lock(cursor_path):
            current = None
            if cursor_path.is_file():
                try:
                    existing = json.loads(cursor_path.read_text(encoding="utf-8"))
                    current = existing.get("value") if isinstance(existing, dict) else None
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    current = None  # unreadable committed file — treat as "no prior value" (I1:
                    # same lenient contract as _cursor_value's sibling read of this file — a
                    # corrupt committed file must never crash a walk after a successful step)
            if current == candidate:
                return None  # unchanged watermark — no rewrite, no event (§4)

            # REAL transition time, not self.now — mirrors _run_gate's gate-commit stamp
            # just above (the resolve_gate call): `updated_at` records when the kernel
            # actually committed, an event, not a rendered/deterministic value, so it is
            # exempt from the "clock is injected" rule that governs path/template `now`.
            doc = {"value": candidate, "updated_at": format_at(datetime.now(timezone.utc))}
            _atomic_write_cursor(cursor_path, doc)

        self._emit(
            "cursor-commit", node=step.id, cycle=cycle, data={"path": step.cursor, "value": candidate}
        )
        return None

    # -- expression evaluation (runtime resolver) --------------------------- #

    def _passes_when(self, when: Expr | None, cycle: int | None, node_id: str) -> bool:
        if when is None:
            return True
        return self._eval(when, cycle, node_id)

    def _eval(self, expr: Expr, cycle: int | None, node_id: str) -> bool:
        try:
            return bool(expr.evaluate(self._resolver(cycle)))
        except EvalError as exc:
            raise _Halt(ExitCode.CONFIG, node_id, f"expression error: {exc}") from exc

    def _guard_active(self, guard: GuardDecl, cycle: int | None) -> bool:
        """Is ``guard`` currently enforced (GUARD-WHEN-PLAN.md §7)? A static guard (``when is
        None``) is always active. A runtime ``when`` is evaluated against the SAME trusted
        resolver steps/gates use (``_resolver``) — ``params``/``dims`` from ``self.plan`` in
        memory, ``gates`` via the W2-verified reader, ``artifacts`` via the contained resolver —
        never re-parsed from the agent-writable run dir.

        Unlike ``_eval`` (steps/gates: an unevaluable ``when`` halts the whole run), a guard whose
        ``when`` raises ``EvalError`` is treated as ACTIVE and a warning is emitted, never halted
        and never silently dropped: over-enforcing on ambiguity is fail-safe, dropping the guard
        would be fail-OPEN (unsafe), and halting the run over one guard's `when` is too aggressive
        for what is, at worst, an over-block."""
        if guard.when is None:
            return True
        try:
            return bool(guard.when.evaluate(self._resolver(cycle)))
        except EvalError as exc:
            self._emit(
                "guard-when-error", node=guard.name, cycle=cycle, data={"error": str(exc)}
            )
            return True

    def _active_guard_manifest(self, step: StepNode, cycle: int | None) -> dict[str, str]:
        """Env overrides pointing this invocation at its per-invocation guard manifest(s), or
        ``{}`` (GUARD-WHEN-PLAN.md §3, §8).

        MANDATORY fast path: if no guard in the plan carries a runtime ``when``, every
        invocation's active set is identical to the once-per-run static install — return ``{}``
        and let the static shim manifest / baked hook fallback stand, byte-identical to pre-C9
        behavior, with zero per-invocation overhead. This is the regression guard for "existing
        (no-runtime-`when`) pipelines are unaffected".

        Otherwise, for each layer the plan actually enforces guards at, evaluate the active
        subset (``_guard_active``) and write a freshly SIGNED manifest (same ``write_manifest``
        builder, same MAC — the subprocess enforcement path is untouched) containing ONLY the
        active guards, at a path keyed by ``step.id`` + ``cycle`` (``gatekeys.guard_manifest_path``
        ``key=``) so concurrent ``parallel:`` children never share a manifest file (§6; retries are
        sequential and a guard's ``when`` doesn't change across attempts of the same step, so
        attempt is not part of the key). The manifest never carries the `when` expression itself
        (`_load_manifest_guard` still sets `when=None` on reload) — inactive guards are simply
        absent, and absence is what `_run_chain` already treats as "skip"."""
        if not any(g.when is not None for g in self.plan.guards):
            return {}

        key = f"{step.id}-c{cycle}"
        overrides: dict[str, str] = {}
        for layer, env_var in (("shim", "CAIRN_SHIM_MANIFEST"), ("hook", "CAIRN_HOOK_MANIFEST")):
            if not any(layer in g.enforce for g in self.plan.guards):
                continue
            active = [
                g for g in self.plan.guards
                if layer in g.enforce and self._guard_active(g, cycle)
            ]
            path = guard_manifest_path(self.run_dir, layer, key=key)
            write_manifest(active, workspace_dir=self.workspace_dir, run_dir=self.run_dir, path=path)
            overrides[env_var] = str(path)
        return overrides

    def _resolver(self, cycle: int | None) -> Callable[[str, tuple[str, ...]], Any]:
        def resolve(root: str, parts: tuple[str, ...]) -> Any:
            if root == "params":
                return self._index(self.plan.params, parts, root)
            if root == "dims":
                return self._index(self.plan.dims, parts, root)
            if root == "cycle":
                if cycle is None:
                    raise EvalError("cycle is not bound outside a loop body")
                return cycle
            if root == "artifacts":
                return self._resolve_artifact_ref(parts, cycle)
            if root == "gates":
                return self._resolve_gate_ref(parts)
            if root == "run":
                return self._index(load_run(self.run_dir), parts, root)
            raise EvalError(f"unknown expression root {root!r}")

        return resolve

    @staticmethod
    def _index(obj: Any, parts: tuple[str, ...], root: str) -> Any:
        if not parts:
            raise EvalError(f"{root} needs a name (e.g. {root}.x)")
        cur = obj
        for i, part in enumerate(parts):
            try:
                cur = cur[part]
            except (KeyError, TypeError, IndexError) as exc:
                dotted = ".".join((root, *parts[: i + 1]))
                raise EvalError(f"unknown path: {dotted}") from exc
        return cur

    def _resolve_artifact_ref(self, parts: tuple[str, ...], cycle: int | None) -> Any:
        if not parts:
            raise EvalError("artifacts needs a name (e.g. artifacts.review.verdict)")
        name = parts[0]
        decl = self.plan.artifacts.get(name)
        if decl is None:
            raise EvalError(f"unknown artifact {name!r} in expression")
        rendered = render_artifact_path(
            decl, params=self.plan.params, dims=self.plan.dims,
            pipeline=self.plan.pipeline, cycle=cycle, now=self.now,
        )
        resolved = resolve_path(decl, rendered, self.run_dir)
        json_files = [p for p in resolved.paths if p.suffix == ".json" and p.is_file()]
        if not json_files:
            raise EvalError(f"artifact {name!r} has no JSON to read at {rendered}")

        try:
            doc = json.loads(json_files[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvalError(f"artifact {name!r} is not readable JSON: {exc}") from exc
        return self._index(doc, parts[1:], f"artifacts.{name}") if parts[1:] else doc

    def _gate_node(self, name: str) -> GateNode | None:
        """The GateNode named ``name`` anywhere in the plan (recursing parallels/loops), or None.

        Needed by every gate-decision reader other than ``resolve_gate`` so it can run the SAME
        MAC verification (the accessor needs the node's ``name`` for the MAC and ``options`` for
        the membership check). Built once, cached.
        """
        if self._gate_nodes is None:
            index: dict[str, GateNode] = {}

            def collect(nodes) -> None:
                for node in nodes:
                    if isinstance(node, GateNode):
                        index[node.name] = node
                    elif isinstance(node, ParallelNode):
                        collect(node.steps)
                    elif isinstance(node, LoopNode):
                        collect(node.body)

            collect(self.plan.nodes)
            self._gate_nodes = index
        return self._gate_nodes.get(name)

    def _resolve_gate_ref(self, parts: tuple[str, ...]) -> Any:
        if not parts:
            raise EvalError("gates needs a name (e.g. gates.tone.choice)")
        name = parts[0]
        gate = self._gate_node(name)
        if gate is None:
            raise EvalError(f"unknown gate {name!r} in expression")
        # Verify the MAC here too: a compromised step can overwrite a legitimately-resolved
        # decision file within the same walk, and `when:` control flow must not trust a forge.
        try:
            doc = read_verified_decision(self.run_dir, gate, self._emit)
        except GateUnanswered as exc:
            raise EvalError(f"gate {name!r} is unanswered") from exc
        except GateTampered as exc:
            raise EvalError(f"gate {name!r} decision failed authentication") from exc
        return self._index(doc, parts[1:], f"gates.{name}") if parts[1:] else doc

    # -- trail / run.json bookkeeping (locked, single-writer) --------------- #

    def _emit(self, event: str, *, node=None, attempt=None, cycle=None, data=None):
        with self._lock:
            return self._trail.emit(event, node=node, attempt=attempt, cycle=cycle, data=data)

    def _set_status(self, node_id: str, status: str, cycles: int | None = None) -> None:
        # The REAL transition time, not self.now (the walk's construction-time clock — kept
        # for path/template rendering determinism elsewhere, see the `now=self.now` call
        # sites above). A node's `at` is an event, not a rendered value: a 3-hour run must
        # not record every node finishing at second zero (claude-F12).
        at = format_at(datetime.now(timezone.utc))
        with self._lock:
            update_run(
                self.run_dir,
                lambda doc: set_node_status(doc, node_id, status, at, cycles=cycles),
            )

    def _mark_run(self, status: str) -> None:
        with self._lock:
            update_run(self.run_dir, lambda doc: doc.__setitem__("status", status))

    # -- agent slots (FACTORY-PLAN §9 / W6) --------------------------------- #

    def _slot_now(self) -> float:
        """Wall clock for slot wait/acquire. Tests inject a fake via assignment."""
        return time.time()

    def _slot_sleep(self, seconds: float) -> None:
        """Sleep during slot wait. Tests inject a fake via assignment."""
        time.sleep(seconds)

    def _slot_beat_interval_s(self) -> float:
        """Cadence for slot-only refresh when trail heartbeats are OFF (default 30s).

        Tests assign a tiny value on the class/instance so a held slot's
        ``refresh_slot`` beat is observable without a real 30s wait.
        """
        return 30.0

    def _acquire_agent_slot(
        self, step: StepNode, *, attempt: int, cycle: int | None
    ) -> str | None:
        """Acquire a factory agent slot for an agent step, or raise CAPACITY.

        Returns None when slots are OFF (no ``[factory] max_agents``). The wait
        happens here — before ``executor.invoke`` — so it is outside
        ``step.timeout_s``. Wait expiry parks the run via ``ExitCode.CAPACITY``.
        """
        max_agents = self.config.factory.max_agents
        if max_agents is None:
            return None

        slots_dir = slots_dir_for(self.workspace_dir)
        wait_s = float(self.config.factory.slot_wait_s)
        waited = {"emitted": False}

        def on_wait_start() -> None:
            if waited["emitted"]:
                return
            waited["emitted"] = True
            self._emit(
                "agent-slot-wait",
                node=step.id,
                attempt=attempt,
                cycle=cycle,
                data={
                    "max_agents": max_agents,
                    "wait_s": wait_s,
                    "slots_dir": str(slots_dir),
                },
            )

        slot = wait_acquire_slot(
            slots_dir,
            max_agents,
            pid=os.getpid(),
            wait_s=wait_s,
            now=self._slot_now,
            sleep=self._slot_sleep,
            poll_s=DEFAULT_POLL_S,
            on_wait_start=on_wait_start,
        )
        if slot is None:
            raise _Halt(
                ExitCode.CAPACITY,
                step.id,
                f"no agent slot within {wait_s:g}s — capacity",
            )
        return slot

    # -- heartbeat (liveness — OBSERVABILITY §1) ---------------------------- #

    @contextmanager
    def _heartbeat(
        self,
        node: str,
        attempt: int,
        cycle: int | None,
        log_path: Path,
        *,
        slot_name: str | None = None,
    ) -> Iterator[None]:
        """Emit a periodic ``heartbeat`` while a blocking step runs.

        Opt-in via ``[defaults] heartbeat`` (config's ``heartbeat_s``); off by default, so
        it is **zero-cost** when unset — no timer thread is even started. When on, a daemon
        timer emits ``heartbeat`` every interval with the step-log byte size + last line +
        elapsed, so ``cairn ps``/``--follow`` can tell a working step from a hung one. The
        first beat waits a full interval, so a step that finishes faster emits none. The
        thread is daemon and joined on exit — it can never outlive the step — and every beat
        goes through the same single-writer trail lock as any other event.

        When ``slot_name`` is set (W6 agent-slot held), each beat also refreshes the
        slot's heartbeat timestamp so a live holder is never reaped as stale.
        """
        interval = self.config.defaults.heartbeat_s
        # Slot refresh needs a beat loop even when trail heartbeats are off — a tiny
        # interval keeps the slot live without emitting trail events when heartbeat_s
        # is unset. When both are off (no slot), zero-cost yield.
        if (not interval or interval <= 0) and not slot_name:
            yield
            return

        stop = threading.Event()
        started = time.monotonic()
        # Trail heartbeats only when configured; slot refresh uses the same cadence
        # (or _slot_beat_interval_s when trail heartbeats are off but a slot is held).
        beat_interval = (
            interval if interval and interval > 0 else self._slot_beat_interval_s()
        )
        emit_trail = bool(interval and interval > 0)
        slots_dir = slots_dir_for(self.workspace_dir) if slot_name else None

        def beat() -> None:
            while not stop.wait(beat_interval):
                if slot_name is not None and slots_dir is not None:
                    refresh_slot(slots_dir, slot_name, now=self._slot_now)
                if not emit_trail:
                    continue
                log_bytes, last_line = _tail_log(log_path)
                if stop.is_set():
                    return  # step finished while we read the log — never emit a stale beat
                self._emit(
                    "heartbeat",
                    node=node,
                    attempt=attempt,
                    cycle=cycle,
                    data={
                        "elapsed_s": round(time.monotonic() - started, 1),
                        "log_bytes": log_bytes,
                        "last_line": last_line,
                    },
                )

        thread = threading.Thread(target=beat, name=f"cairn-heartbeat-{node}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()  # Event.wait returns at once → the daemon exits without a full interval.
            thread.join(timeout=5)

    def _write_step_return_schema(self) -> None:

        self._schema_path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_path.write_text(
            json.dumps(get_schema("step-return"), indent=2), encoding="utf-8"
        )

    # -- small helpers ------------------------------------------------------ #

    def _log_path(self, step_id: str, ext: str, attempt: int, cycle: int | None) -> Path:
        stem = step_id
        if attempt > 1:
            stem += f".r{attempt}"
        if cycle is not None:
            stem += f".c{cycle}"
        return self.run_dir / "logs" / f"{stem}.{ext}"

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.run_dir))
        except ValueError:
            return str(path)

    @staticmethod
    def _node_id(node: Any) -> str:
        return node.id if isinstance(node, StepNode) else node.name


def _tail_log(path: Path, tail_bytes: int = 4096) -> tuple[int, str]:
    """(byte size, last non-empty line) of a step log — bounded-cost, never raises.

    Only the final ``tail_bytes`` are read, so a heartbeat over a multi-hour step's log stays
    cheap. A missing/partway-written/unreadable log degrades to ``(0, "")`` — a heartbeat is
    a liveness ping, never a hard dependency.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0, ""
    last = ""
    try:
        with path.open("rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()  # drop the partial first line after the seek
            chunk = fh.read()
    except OSError:
        return size, ""
    for raw in chunk.splitlines():
        line = raw.decode("utf-8", "replace").strip()
        if line:
            last = line
    return size, last
