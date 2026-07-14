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
from pathlib import Path

from cairn.kernel.errors import CairnError, ExecutorSpawnError
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


# The STEP sentinel: a chatty model frames its final machine-readable block between these
# markers so it survives surrounding prose (docs/API.md §7). ``\b`` after STEP keeps a bare
# ``<<<STEP{...}`` from matching the closing marker's ``STEP``.
_STEP_RE = re.compile(r"<<<STEP\b(.*?)STEP>>>", re.DOTALL)


def parse_step_sentinel(text: str) -> dict | None:
    """Extract the LAST well-formed ``<<<STEP … STEP>>>`` block and json.loads it.

    Returns the parsed object, or None when absent / unparsable / not a JSON object.
    Authority rule (docs/ARCHITECTURE.md §7): artifact validation outranks this block, so a
    missing or malformed STEP is a soft signal (→ None), never a hard failure here.
    """
    if not text:
        return None
    # Scan matched blocks last→first; the LAST well-formed (json-object) block wins, so a
    # trailing broken/partial block never masks a good one earlier in the output.
    for block in reversed(_STEP_RE.findall(text)):
        try:
            obj = json.loads(block)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
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
    log or captured — so declared secret values never land on disk in ``logs/<step>.log`` (and
    never in the returned output the walker parses for the STEP block). It runs per line in the
    pump thread; a secret split across two lines is not caught (tokens are single-line), and the
    trail's own redactor is the belt-and-suspenders scrub for any event payload. ``None`` ⇒ the
    stream is teed verbatim, byte-for-byte as before.
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
    return proc.returncode, "".join(captured), time.monotonic() - start
