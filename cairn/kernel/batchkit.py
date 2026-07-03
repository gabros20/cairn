"""The batch engine — an outer process pool of ``cairn run``s (docs/CONCEPTS.md §batch).

Batch is deliberately *not* a kernel concept: it spawns one ``cairn run <pipeline>
--headless`` SUBPROCESS per line of a JSONL params file, each in its own run dir, bounded
by a ``-j`` process pool. Per-process isolation — not an in-process ``walk()`` fan-out — is
the whole point: each child gets its own guard env, its own run-dir flock, and crash
containment (one child segfaulting can't take the fleet down). The children own run-dir
naming, uniqueness (auto ``-v2`` on collision), and locking via the normal machinery; this
module just pools them and collects results.

stdlib only. Threads (not asyncio) pool the subprocess-bound children — the doctrine's
"threads for parallel groups suffice".
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from cairn.kernel.errors import ConfigError
from cairn.kernel.proc import SubprocessRunner
from cairn.kernel.types import ExitCode

# The one production runner behind default_spawn — the shared subprocess seam (proc.py), the
# same implementation schedkit injects as a Runner. Stateless, so a module-level singleton.
_RUNNER = SubprocessRunner()

# Sensible fallback pool size; the CLI passes ``-j`` explicitly (docs/API.md shows ``-j 8``).
DEFAULT_JOBS = 4

# spawn(argv, cwd) -> (exit_code, stdout, stderr). Injected in tests; default = default_spawn,
# a thin adapter over the shared subprocess seam (cairn.kernel.proc.SubprocessRunner — the same
# implementation schedkit injects as a Runner). Batch's seam is a bare callable returning a TUPLE
# (not schedkit's Runner object / RunResult) because the per-line thread pool wants a tuple and
# batch never pipes stdin; proc.py documents the two-shapes-one-implementation split.
# stdout/stderr are kept SEPARATE (not merged): the ``→ run_dir`` marker lands on stdout for a
# success but on stderr for a failure/awaiting-human (cli.py), so parse_run_dir sees both, while
# the failure tail (RunOutcome.error) is drawn from stderr alone.
Spawn = Callable[[list[str], Path], "tuple[int, str, str]"]
Clock = Callable[[], float]

# Bounds on the failed-child stderr tail retained in RunOutcome.error. A runaway child can flood
# megabytes to stderr; subprocess buffers that transiently, but every RunOutcome lives for the
# whole batch (one per line, all held at once), so we keep only a small legible TAIL — the end,
# where the actual failure (gate reason / config / executor error) is — never the whole stream.
_ERROR_TAIL_MAX_LINES = 20
_ERROR_TAIL_MAX_CHARS = 2000  # str characters (not bytes) — a simple cap that can't split a codepoint


# --------------------------------------------------------------------------- #
# Result types.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RunOutcome:
    """One child run's result. ``index`` is the 0-based input line order (summary is sorted
    by it, never by completion order). ``run_dir`` is parsed from the child's terminal
    marker line; None if the child printed none (e.g. it crashed before bootstrapping).
    ``error`` carries diagnostic text for a FAILED run and is None for a success. Two ways it
    is populated: (1) the SPAWN itself raised (missing interpreter, bad cwd) → exit_code=EXECUTOR
    and error = the exception text; (2) the child STARTED but exited non-zero → error = a bounded
    tail of its stderr (the actual failure reason: gate/config/executor error). A child that
    exited 0, or one that failed silently with no stderr, keeps error=None."""

    index: int
    params: dict
    run_dir: Path | None
    exit_code: int
    duration_s: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class BatchResult:
    """The aggregate. ``outcomes`` is in input order (deterministic). ``exit_code`` is the
    aggregate rule (see :func:`aggregate_exit_code`)."""

    pipeline: str
    outcomes: tuple[RunOutcome, ...]
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def failed(self) -> tuple[RunOutcome, ...]:
        return tuple(o for o in self.outcomes if not o.ok)


# --------------------------------------------------------------------------- #
# Params file — eager JSONL validation (bad input fails before anything spawns).
# --------------------------------------------------------------------------- #


def load_param_sets(params_file: Path) -> list[dict]:
    """Parse a JSONL params file into one dict per non-blank line.

    Every non-blank line must be a JSON *object*; bad JSON or a non-object (array, string,
    number) raises ConfigError naming the line. Blank/whitespace-only lines are ignored. An
    empty file (no runs) is itself a ConfigError — a batch of nothing is operator error.
    """
    params_file = Path(params_file)
    try:
        text = params_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read params file {params_file}: {exc}") from exc

    sets: list[dict] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{params_file}:{lineno}: not valid JSON: {exc.msg}") from exc
        if not isinstance(obj, dict):
            raise ConfigError(
                f"{params_file}:{lineno}: each line must be a JSON object, got {type(obj).__name__}"
            )
        sets.append(obj)

    if not sets:
        raise ConfigError(f"params file {params_file} has no runs (no non-blank lines)")
    return sets


# --------------------------------------------------------------------------- #
# Child command construction + spawning.
# --------------------------------------------------------------------------- #


def _stringify(value: object) -> str:
    """Params values become ``--param k=v`` strings (matches the CLI's own coercion — a JSON
    ``true`` stringifies to ``"True"``; operators should quote enum-ish values in the JSONL)."""
    return value if isinstance(value, str) else str(value)


def build_run_argv(
    pipeline: str,
    params: dict,
    gate_presets: dict[str, str],
    extra_args: list[str],
) -> list[str]:
    """The production child argv: ``python -m cairn run <pipeline> --headless`` + params +
    gates + passthrough. Spawned with ``cwd`` = the workspace (``run`` has no ``--workspace``
    flag), so the child resolves the workspace and its run dir exactly as an interactive run.
    """
    argv = [sys.executable, "-m", "cairn", "run", pipeline, "--headless"]
    for key, value in params.items():
        argv += ["--param", f"{key}={_stringify(value)}"]
    for name, choice in gate_presets.items():
        argv += ["--gate", f"{name}={choice}"]
    argv += list(extra_args)
    return argv


def default_spawn(argv: list[str], cwd: Path) -> tuple[int, str, str]:
    """Run one child to completion, capturing stdout and stderr SEPARATELY. Never raises on a
    non-zero child — the exit code IS the signal batch collects. The streams are kept apart so
    the failure reason (stderr) can be surfaced without the marker parse depending on a clean
    stdout: the ``→ run_dir`` marker is on stdout for a success but on stderr for a failure.

    A thin adapter over the shared :class:`cairn.kernel.proc.SubprocessRunner` (the same seam
    schedkit injects as a ``Runner``): the subprocess capture lives once in proc.py; batch's
    ``spawn`` seam stays a bare ``(argv, cwd) -> (int, str, str)`` callable because that tuple
    shape is what the per-line thread pool wants, and batch never pipes stdin (schedkit's
    ``input=``) — so a bare callable, not a Runner object, is the right injection here."""
    result = _RUNNER.run(argv, cwd=cwd)
    return result.returncode, result.stdout, result.stderr


def _error_tail(stderr: str) -> str | None:
    """The bounded, legible tail of a failed child's stderr, or None if it wrote nothing.

    Keeps the LAST ``_ERROR_TAIL_MAX_LINES`` lines then caps to ``_ERROR_TAIL_MAX_CHARS``
    characters (from the end) — the end is where the actual failure surfaces, and the cap keeps
    a runaway child from ballooning the RunOutcome held for the whole batch."""
    text = stderr.strip()
    if not text:
        return None
    lines = text.splitlines()
    if len(lines) > _ERROR_TAIL_MAX_LINES:
        lines = lines[-_ERROR_TAIL_MAX_LINES:]
    tail = "\n".join(lines)
    if len(tail) > _ERROR_TAIL_MAX_CHARS:
        tail = tail[-_ERROR_TAIL_MAX_CHARS:]
    return tail


def parse_run_dir(output: str) -> Path | None:
    """Extract the run dir from a child's terminal marker line.

    The CLI prints ``cairn: … → <run_dir>`` on every terminal outcome (complete / halt /
    awaiting-human). The awaiting-human line trails a ``  (answer …)`` note, stripped here.
    The LAST such line wins (the terminal outcome is last). None if no marker is found.
    """
    found: str | None = None
    for line in output.splitlines():
        if "cairn:" in line and "→ " in line:
            rest = line.split("→ ", 1)[1]
            rest = rest.split("  (", 1)[0].strip()  # drop the awaiting-human annotation
            if rest:
                found = rest
    return Path(found) if found else None


# --------------------------------------------------------------------------- #
# Aggregate exit-code rule (PINNED).
# --------------------------------------------------------------------------- #


def aggregate_exit_code(exit_codes: list[int]) -> int:
    """PINNED: 0 iff every run exited 0; otherwise the HIGHEST exit code among failed runs.

    The rule is plain ``max`` over the failing codes — deterministic regardless of completion
    order, and roughly tracking the ExitCode enum's escalation (CONFIG=2 … NEEDS_HUMAN=6,
    BUDGET=7). It is a tie-break heuristic, not a strict severity ranking (BUDGET outranks
    NEEDS_HUMAN under max); per-run codes are in the outcomes for anything finer.
    """
    failures = [c for c in exit_codes if c != 0]
    return max(failures) if failures else 0


# --------------------------------------------------------------------------- #
# The pool.
# --------------------------------------------------------------------------- #


def run_batch(
    workspace_dir: Path,
    pipeline: str,
    params_file: Path,
    *,
    jobs: int = DEFAULT_JOBS,
    gate_presets: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
    spawn: Spawn = default_spawn,
    out: TextIO | None = None,
    clock: Clock = time.monotonic,
) -> BatchResult:
    """Spawn one ``cairn run --headless`` child per JSONL line, pooled at ``jobs`` at a time.

    Failure policy (PINNED): a failing run NEVER cancels its siblings — batch is for fleets,
    so every line always gets its subprocess and the aggregate reports the worst outcome.
    Summary order (PINNED): input line order, independent of completion order. One progress
    line is streamed to ``out`` per completion.
    """
    workspace_dir = Path(workspace_dir)
    gate_presets = gate_presets or {}
    extra_args = list(extra_args or [])
    out = out if out is not None else sys.stdout

    if jobs < 1:
        raise ConfigError(f"batch needs jobs >= 1, got {jobs}")

    param_sets = load_param_sets(params_file)  # eager: bad JSONL fails before any spawn

    def one(index: int, params: dict) -> RunOutcome:
        argv = build_run_argv(pipeline, params, gate_presets, extra_args)
        start = clock()
        try:
            code, stdout, stderr = spawn(argv, workspace_dir)
        except Exception as exc:  # noqa: BLE001 — crash containment IS the failure policy:
            # a spawn that raises (missing interpreter, bad cwd, OS error) becomes a failed
            # outcome, never a fleet abort — siblings keep running.
            return RunOutcome(
                index=index,
                params=params,
                run_dir=None,
                exit_code=int(ExitCode.EXECUTOR),
                duration_s=clock() - start,
                error=f"{type(exc).__name__}: {exc}",
            )
        duration = clock() - start
        # Marker can be on EITHER stream (stdout for success, stderr for failure/awaiting-human),
        # so parse over both; the failure tail is drawn from stderr alone, only when the child failed.
        combined = stdout + "\n" + stderr
        return RunOutcome(
            index=index,
            params=params,
            run_dir=parse_run_dir(combined),
            exit_code=code,
            duration_s=duration,
            error=_error_tail(stderr) if code != 0 else None,
        )

    results: dict[int, RunOutcome] = {}
    done = 0
    total = len(param_sets)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(one, i, p): i for i, p in enumerate(param_sets)}
        for fut in as_completed(futures):
            outcome = fut.result()
            results[outcome.index] = outcome
            done += 1
            _progress(out, done, total, outcome)

    outcomes = tuple(results[i] for i in range(total))
    exit_code = aggregate_exit_code([o.exit_code for o in outcomes])
    return BatchResult(pipeline=pipeline, outcomes=outcomes, exit_code=exit_code)


def _progress(out: TextIO, done: int, total: int, o: RunOutcome) -> None:
    rd = o.run_dir.name if o.run_dir is not None else "?"
    # ONE line per completion (pinned): compact a multi-line error tail to a one-line preview;
    # the full tail is rendered in the CLI's end-of-batch summary block.
    tail = f"  {_preview_line(o.error)}" if o.error else ""
    out.write(f"[{done}/{total}] {rd}  exit {o.exit_code}  {o.duration_s:.1f}s{tail}\n")
    out.flush()


def _preview_line(error: str) -> str:
    """The one-line preview of an error tail: the LAST line — where tracebacks and error
    messages terminate — except a trailing run-outcome marker (``cairn: … → run_dir``), which
    the progress line already conveys (run dir + exit code); skip past those to the last
    substantive line. Falls back to the true last line if the tail is all marker."""
    lines = error.splitlines()
    for line in reversed(lines):
        if not ("cairn:" in line and "→ " in line):
            return line
    return lines[-1]
