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

The five node kinds (ARCHITECTURE §3.1-3.6):

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

Out of scope here (TODO): budgets, heartbeat, trail sinks, guard shims, redaction.
Stdlib + pinned kernel modules only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import cairn
from cairn.executors.base import ExecTimeout
from cairn.kernel.artifacts import (
    DEFAULT_VALIDATOR_TIMEOUT_S,
    done,
    exists,
    resolve_path,
    validate,
)
from cairn.kernel.compose import render_artifact_path
from cairn.kernel.config import Config
from cairn.kernel.errors import CairnError
from cairn.kernel.expr import EvalError, Expr
from cairn.kernel.gatekit import GateNeedsHuman, gate_path, resolve_gate
from cairn.kernel.plan import (
    GateNode,
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
from cairn.kernel.template import HELPERS, TemplateContext, TemplateError, render
from cairn.kernel.types import ExitCode, Invocation

# The template placeholder + helper shapes, mirrored here so lenient command rendering can
# decide per-``{…}`` whether a brace is cairn's (substitute) or foreign (pass through).
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_HELPER_CALL = re.compile(r"(\w+)\((.*)\)")
_VALUE_KEYWORDS = frozenset({"pipeline", "date", "datetime", "cycle"})


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
            "created_at": now.isoformat(),
            "status": "running",
            "nodes": {},
        }

    if run_dir is not None:
        run_dir = Path(run_dir)
        return create_run(run_dir.parent, run_dir.name, payload(run_dir.name))

    root = Path(runs_root) if runs_root is not None else workspace_dir / "runs"
    base = render_run_id(plan, now)
    run_id = base
    suffix = 1
    while True:
        try:
            return create_run(root, run_id, payload(run_id))
        except RunExistsError:
            suffix += 1
            run_id = f"{base}-v{suffix}"


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
) -> ExitCode:
    """Walk ``plan`` against ``run_dir`` to completion or the first halt.

    Idempotent per node, so calling it again on the same run dir *is* resume. The whole
    walk holds the run's advisory lock; a concurrent holder makes this return
    ``ExitCode.EXECUTOR`` with a stderr message rather than interleaving.
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

    def __post_init__(self) -> None:
        # One RLock serializes every trail append AND every run.json mutation, so N parallel
        # children keep the trail single-writer and the manifest consistent (OBSERVABILITY §1).
        self._lock = threading.RLock()
        self._trail = None  # set in run()
        self._schema_path = self.run_dir / ".cairn" / "step-return.json"

    # -- top-level orchestration -------------------------------------------- #

    def run(self) -> ExitCode:
        from cairn.kernel.trail import TrailWriter

        run_doc = load_run(self.run_dir)
        run_id = run_doc["run_id"]
        self._write_step_return_schema()

        with TrailWriter(self.run_dir, run_id) as trail:
            self._trail = trail
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
            try:
                for node in self.plan.nodes:
                    self._dispatch(node, None)
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

            self._emit("run-done", data={"nodes": len(self.plan.nodes)})
            self._mark_run("done")
            return ExitCode.OK

    def _install_guards(self) -> None:
        seen: set[int] = set()
        for ex in self.executors.values():
            if id(ex) in seen:
                continue
            seen.add(id(ex))
            ex.install_guards(self.plan.guards, self.workspace_dir)

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
                return self._render_command(step.command or "", cycle, step.id)
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
            )
            try:
                result = executor.invoke(inv)
            except ExecTimeout as exc:
                self._emit("timeout", node=step.id, attempt=attempt, cycle=cycle, data={"error": str(exc)})
                raise _Halt(ExitCode.TIMEOUT, step.id, f"timeout: {exc}") from exc
            except CairnError as exc:
                self._emit("step-fail", node=step.id, attempt=attempt, cycle=cycle, data={"error": str(exc)})
                raise _Halt(ExitCode.EXECUTOR, step.id, f"executor failure: {exc}") from exc

            block = result.step or {}
            for learn in block.get("learnings") or []:
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
            if step.kind == "run" and result.exit_code != 0:
                ok = False
                validator_reasons = validator_reasons + [f"command exited with code {result.exit_code}"]

            if ok:
                data: dict[str, Any] = {
                    "artifacts": self._produced_paths(step, cycle),
                    "duration_s": result.duration_s,
                }
                if block.get("metrics"):
                    data["metrics"] = block["metrics"]
                if block.get("usage"):
                    data["usage"] = block["usage"]
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
        rendered = self._render_command(step.command or "", cycle, step.id)
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
            resolve_gate(
                gate,
                self.run_dir,
                interactive=self.interactive,
                presets=self.gate_presets,
                emit=self._emit,
                now=self.now,
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
        """Largest N with cycles 1..N ALL having every body ``produces`` valid (re-validated)."""
        decls = [
            (name, self.plan.artifacts[name])
            for child in node.body
            if isinstance(child, StepNode)
            for name in child.produces
            if name in self.plan.artifacts
        ]
        if not decls:
            return 0
        n = 0
        while True:
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
                if not gate_path(self.run_dir, name).is_file():
                    raise _Halt(ExitCode.CONFIG, step.id, f"required gate {name!r} is unanswered")

    # -- environment (SECURITY §1.2 — deny by default) ---------------------- #

    def _build_env(self, step: StepNode) -> dict[str, str]:
        env: dict[str, str] = {}
        # USER/LOGNAME are identity, not secrets: a macOS Keychain lookup (how `claude`/`codex`
        # find their stored OAuth credential) needs USER, so stripping it makes every executor
        # report "Not logged in". Kept in the deny-by-default baseline for that reason.
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME"):
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

    def _render_command(self, command: str, cycle: int | None, node_id: str) -> str:
        ctx = self._command_context(cycle)

        def substitute(match: re.Match) -> str:
            body = match.group(1).strip()
            if not self._is_cairn_placeholder(body):
                return match.group(0)  # foreign braces (jq/awk/python literal) — verbatim
            try:
                return render(match.group(0), ctx)
            except TemplateError as exc:
                raise _Halt(ExitCode.CONFIG, node_id, f"command placeholder {{{body}}}: {exc}") from exc

        return _PLACEHOLDER.sub(substitute, command)

    def _command_context(self, cycle: int | None) -> TemplateContext:
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
            path = gate_path(self.run_dir, name)
            if not path.is_file():
                raise KeyError(name)

            return json.loads(path.read_text(encoding="utf-8")).get("choice")

        return TemplateContext(
            params=self.plan.params,
            dims=self.plan.dims,
            pipeline=self.plan.pipeline,
            cycle=cycle,
            now=self.now,
            artifact=artifact,
            gate=gate,
            run_dir=str(self.run_dir),
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
        return body.startswith("params.") or body.startswith("dims.")

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

    def _resolve_gate_ref(self, parts: tuple[str, ...]) -> Any:
        if not parts:
            raise EvalError("gates needs a name (e.g. gates.tone.choice)")
        name = parts[0]
        path = gate_path(self.run_dir, name)
        if not path.is_file():
            raise EvalError(f"gate {name!r} is unanswered")

        doc = json.loads(path.read_text(encoding="utf-8"))
        return self._index(doc, parts[1:], f"gates.{name}") if parts[1:] else doc

    # -- trail / run.json bookkeeping (locked, single-writer) --------------- #

    def _emit(self, event: str, *, node=None, attempt=None, cycle=None, data=None):
        with self._lock:
            return self._trail.emit(event, node=node, attempt=attempt, cycle=cycle, data=data)

    def _set_status(self, node_id: str, status: str, cycles: int | None = None) -> None:
        at = self.now.isoformat()
        with self._lock:
            update_run(
                self.run_dir,
                lambda doc: set_node_status(doc, node_id, status, at, cycles=cycles),
            )

    def _mark_run(self, status: str) -> None:
        with self._lock:
            update_run(self.run_dir, lambda doc: doc.__setitem__("status", status))

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
