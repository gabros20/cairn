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
from pathlib import Path

from cairn.kernel.errors import CairnError
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
    blocks = _STEP_RE.findall(text)
    if not blocks:
        return None
    try:
        obj = json.loads(blocks[-1])
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


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
) -> tuple[int, str, float]:
    """Run one subprocess with EXACTLY ``env`` (never os.environ), streaming combined
    stdout+stderr to ``log_path`` as it arrives while also capturing it.

    Returns ``(exit_code, captured_output, duration_s)``. On timeout the process group is
    killed and :class:`ExecTimeout` is raised. Never uses ``shell=True``.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    captured: list[str] = []
    start = time.monotonic()

    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(cwd),
        env=dict(env),
        text=True,
        bufsize=1,
        start_new_session=True,  # own process group so a timeout can group-kill children
    )

    def _pump() -> None:
        assert proc.stdout is not None
        with open(log_path, "w", encoding="utf-8") as log:
            for line in proc.stdout:
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

    reader.join(timeout_s)
    if reader.is_alive() or proc.poll() is None:
        _kill_process_group(proc)
        reader.join(2)
        raise ExecTimeout(
            f"{argv[0]} exceeded timeout of {timeout_s}s after "
            f"{time.monotonic() - start:.1f}s"
        )

    proc.wait()
    return proc.returncode, "".join(captured), time.monotonic() - start
