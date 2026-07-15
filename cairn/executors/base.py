"""The stable surface for executor authors.

Third-party executors implement the :class:`Executor` protocol and register under the
``cairn.executors`` entry point. Everything an author needs is re-exported here so they
import from one place and never reach into kernel internals.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

import jsonschema

from cairn.kernel.errors import CairnError, ExecutorSpawnError
from cairn.kernel.schemas import get_schema
from cairn.kernel.types import (
    EFFORTS,
    TIERS,
    Capabilities,
    Executor,
    Finding,
    Invocation,
    Result,
)

__all__ = [
    "Executor",
    "Capabilities",
    "Invocation",
    "Result",
    "Finding",
    "TIERS",
    "EFFORTS",
    "ExecTimeout",
    "ExecutorSpawnError",
    "parse_step_sentinel",
    "run_process",
]


class ExecTimeout(CairnError):
    """An invocation exceeded its ``timeout_s``. The walker maps this to ExitCode.TIMEOUT."""


# The STEP sentinel: a chatty model frames its final machine-readable block after this opening
# marker so it survives surrounding prose (docs/API.md §7). ``\b`` after STEP keeps a bare
# ``<<<STEP{...}`` from matching mid-word (e.g. a stray ``<<<STEPPER``). The closing ``STEP>>>``
# is NOT used to delimit the block — see the raw_decode note in parse_step_sentinel below.
_STEP_START_RE = re.compile(r"<<<STEP\b")


@lru_cache(maxsize=1)
def _relaxed_step_schema() -> dict:
    """The bundled step-return schema with its top-level ``required`` dropped.

    Property TYPES are still enforced (e.g. a learnings item must be an object with ``note``,
    so ``{"learnings": ["x"]}`` is rejected), but PRESENCE is not (so a status-only block like
    ``{"status": "blocked", "summary": "…"}`` with no ``artifacts`` still validates). This is
    what lets a well-typed-but-incomplete soft signal through while still rejecting a
    wrong-shaped one (codex-F10).
    """
    schema = dict(get_schema("step-return"))
    schema.pop("required", None)
    return schema


def parse_step_sentinel(text: str) -> dict | None:
    """Extract the LAST schema-valid ``<<<STEP …`` block and return it as a dict.

    For each ``<<<STEP`` marker (scanned last→first), the first complete JSON value after it is
    read with ``json.JSONDecoder().raw_decode`` — real JSON parsing, so a ``STEP>>>`` marker
    appearing INSIDE a string in the payload (e.g. a summary that quotes the protocol back) is
    just string content, not a terminator (claude-F13). A trailing ``STEP>>>`` after the object
    is harmless: raw_decode stops at the value's closing brace and the rest is ignored.

    The result is validated against a relaxed copy of the step-return schema (required dropped,
    types enforced — see ``_relaxed_step_schema``) before it is accepted, so a wrong-shaped
    object (e.g. ``{"learnings": ["x"]}``) can never reach the walker (codex-F10).

    Returns the parsed object, or None when absent / unparsable / not a schema-valid JSON
    object. Authority rule (docs/ARCHITECTURE.md §7): artifact validation outranks this block, so
    a missing or malformed STEP is a soft signal (→ None), never a hard failure here.
    """
    if not text:
        return None
    decoder = json.JSONDecoder()
    schema = _relaxed_step_schema()
    starts = [m.end() for m in _STEP_START_RE.finditer(text)]
    # Scan markers last→first; the LAST schema-valid block wins, so a trailing broken/partial
    # block never masks a good one earlier in the output.
    for start in reversed(starts):
        idx = start
        while idx < len(text) and text[idx].isspace():
            idx += 1
        try:
            obj, _end = decoder.raw_decode(text, idx)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        try:
            jsonschema.validate(obj, schema)
        except jsonschema.ValidationError:
            continue
        return obj
    return None


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the whole session started with ``start_new_session=True`` (kills children too)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def run_process(
    argv: list[str],
    *,
    stdin_text: str | None,
    env: dict[str, str],
    cwd: Path,
    timeout_s: float,
    log_path: Path,
    redactor: Callable[[str], str] | None = None,
) -> tuple[int, str, float]:
    """Run one subprocess with EXACTLY ``env`` (never os.environ), streaming combined
    stdout+stderr to ``log_path`` as it arrives while also capturing it.

    Returns ``(exit_code, captured_output, duration_s)``. On timeout the process group is
    killed and :class:`ExecTimeout` is raised. Never uses ``shell=True``.

    ``redactor`` (SECURITY.md §1.3), when given, scrubs each line *before* it is written to the
    log or captured, AND is re-applied once, whole-content, on both the log and the returned
    text after the process exits (W6-C, grok-F10). The per-line pass alone cannot catch a
    secret whose value is SPLIT across a newline (tokens are single-line) — it would land raw
    on disk and in the captured text the walker parses for the STEP block. The final
    whole-content pass closes that gap: the log is one bounded step's output, so re-reading and
    rewriting it once here is cheap, and the captured string is complete by then. ``None`` ⇒ the
    stream is teed verbatim, byte-for-byte as before (no whole-content pass either).
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    captured: list[str] = []
    start = time.monotonic()

    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(cwd),
            env=dict(env),
            text=True,
            errors="replace",  # one non-utf8 byte from a CLI must not kill the pump thread
            bufsize=1,
            start_new_session=True,  # own process group so a timeout can group-kill children
        )
    except OSError as exc:
        # A missing/non-executable binary (or a bad cwd) raises here, before there is any
        # process to wait on. Type it so the walker's `except CairnError` (walk.py) maps it
        # to a run-halt at exit 4 instead of an uncaught traceback that leaves run.json
        # stuck "running" forever (codex-F14/claude-F4). Never log the full argv/env — either
        # can carry secrets (SECURITY §1.3) — the executable name + errno is enough to act on.
        exe = argv[0] if argv else "<empty>"
        raise ExecutorSpawnError(f"{exe!r} failed to start: {exc.strerror or exc}", executable=exe) from exc

    def _pump() -> None:
        assert proc.stdout is not None
        with open(log_path, "w", encoding="utf-8") as log:
            for line in proc.stdout:
                if redactor is not None:
                    line = redactor(line)
                captured.append(line)
                log.write(line)
                log.flush()

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()

    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text)
        except BrokenPipeError:
            pass
        finally:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass

    # Wait on the PROCESS for the budget — never on the pipe. Clocking the pipe misreads
    # both directions: a fast step can hit stdout EOF microseconds before the OS reaps it
    # (``poll()`` → ``None`` → a 0.8s step reported as "exceeded timeout of 1800s"), and a
    # step that backgrounds a child leaves the pipe open long after the step itself exited
    # (reader alive → budget burned, then a false timeout that group-kills the survivor).
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        reader.join(2)
        # Reap the killed process (and re-kill best-effort if it lingers) so a long-lived
        # walker doesn't accumulate defunct children.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        raise ExecTimeout(
            f"{argv[0]} exceeded timeout of {timeout_s}s after "
            f"{time.monotonic() - start:.1f}s"
        )

    # Exited within budget. Give the pump a short, bounded grace to drain what's already
    # buffered; a backgrounded grandchild may hold the pipe open indefinitely, in which case
    # the daemon thread keeps teeing its output to the log while the step result returns now.
    reader.join(2)
    captured_text = "".join(captured)
    if redactor is not None:
        # Whole-content pass (W6-C): the captured string is complete now, so a secret that
        # was split across a newline in the streamed output — and so survived the per-line
        # pass above — is still caught here, before the walker parses this text for the STEP
        # block. Redacting an already-redacted marker is a no-op, so this is safe to layer on.
        captured_text = redactor(captured_text)
        try:
            log_path.write_text(redactor(log_path.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
        except OSError:
            pass  # best-effort — a log rewrite failure must not fail an otherwise-good step
    return proc.returncode, captured_text, time.monotonic() - start
