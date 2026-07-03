"""proc — the one production subprocess seam shared by batchkit and schedkit.

Two kits need the same primitive: run a child process and capture ``(returncode, stdout,
stderr)``. Rather than three near-identical ``subprocess.run(..., capture_output=True,
text=True)`` call sites (schedkit's ``Runner``, the CLI's ``_SubprocessRunner``, and
batchkit's ``default_spawn``), the capture happens ONCE here, behind an injectable boundary.

Two consumers, two idiomatic injection shapes over the SAME implementation:

- schedkit injects a :class:`Runner` object — it needs ``input=`` (piping crontab text to
  ``crontab -``) and an optional ``cwd`` (``crontab -l`` has none), which a bare
  ``(argv, cwd)`` callable can't express. Its side effects (crontab / launchctl / systemctl
  / a child ``cairn``) all flow through ``runner.run(...)``.
- batchkit injects a bare ``spawn(argv, cwd) -> (int, str, str)`` callable, one per JSONL
  line into a thread pool; a tuple-returning callable is the natural per-line shape there.
  ``batchkit.default_spawn`` is a thin adapter over :class:`SubprocessRunner` (see there),
  so the production subprocess logic still lives here, once.

:class:`RunResult` and :class:`Runner` are re-exported from ``schedkit`` for back-compat.

stdlib only.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class RunResult:
    """The outcome of one host-command invocation: exit code + captured streams."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    """The injected effect boundary — the only thing that actually touches the host.

    Tests pass a fake; production passes :class:`SubprocessRunner`. Keeping this the sole
    side-effect surface is what lets every render/plan path stay pure and offline.
    """

    def run(self, argv: list[str], *, input: str | None = None, cwd: Path | None = None) -> RunResult:
        ...


class SubprocessRunner:
    """The one production :class:`Runner` — runs a child, capturing stdout/stderr SEPARATELY.

    Never raises on a non-zero child: the exit code IS the signal callers collect. ``input``
    is piped to the child's stdin when given (schedkit's ``crontab -``); ``cwd`` scopes the
    run to a directory when given. Streams stay apart so a failure reason (stderr) can be
    surfaced without the caller's parse depending on a clean stdout.
    """

    def run(self, argv: list[str], *, input: str | None = None, cwd: Path | None = None) -> RunResult:
        proc = subprocess.run(
            [str(a) for a in argv],
            input=input,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
        )
        return RunResult(returncode=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or "")
