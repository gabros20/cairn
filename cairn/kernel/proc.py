"""proc — the one production subprocess seam shared by batchkit and schedkit.

Two kits need the same primitive: run a child process and capture ``(returncode, stdout,
stderr)``. Rather than three near-identical ``subprocess`` call sites (schedkit's
``Runner``, the CLI's ``_SubprocessRunner``, and batchkit's ``default_spawn``), the
capture happens ONCE here, behind an injectable boundary.

The primitive is :meth:`Runner.spawn` → :class:`ProcessHandle` (pid available
immediately, wait later). Blocking :meth:`Runner.run` is the convenience
implemented over it (``spawn(...).wait()``). One protocol, one seam — never two
APIs side by side (FACTORY-PLAN D10). The drain's crash-safety path (FACTORY-PLAN
T5) needs the pid *before* exit; that is why spawn is the primitive.

**Pipe-drain contract.** Production ``spawn`` PIPEs both stdout and stderr.
Streams are drained **only** by :meth:`ProcessHandle.wait` (via ``communicate``).
:meth:`ProcessHandle.poll` is a liveness probe and does **not** drain pipes. A
caller that polls without a pending ``wait()`` will deadlock once the child
fills the OS pipe buffer (~16–64 KiB). Safe patterns: (1) call ``wait()``
promptly after recording the pid; (2) the factory drain pattern — a pool thread
blocks in ``wait()`` while other code reads pid/liveness via ``pid``/``poll``;
(3) bound the child's output so it cannot fill the pipe before ``wait()``.

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


class ProcessHandle(Protocol):
    """A live (or finished) child process with an immediately-available pid.

    This is the drain's crash-safety seam (FACTORY-PLAN T5): the pid must be
    observable BEFORE exit so a host-woken drain can record it in the lease
    before the child terminates. ``wait`` captures streams; ``poll`` is
    non-blocking; ``terminate`` is the cooperative kill path.

    **Reaping ownership.** Whoever holds the handle owns reaping: eventually
    call :meth:`wait` (preferred — also drains streams) or :meth:`poll` until a
    non-``None`` exit code. An unreaped handle leaks a zombie until this process
    exits; on crash/exit, orphans reparent to init, which reaps them. Do not rely
    on handle finalizers — explicit reaping is the contract.
    """

    @property
    def pid(self) -> int:
        """OS process id, available as soon as :meth:`Runner.spawn` returns."""
        ...

    def wait(self, timeout: float | None = None) -> RunResult:
        """Block until exit (optional timeout); return code + captured streams.

        The **only** method that drains the child's stdout/stderr pipes. Never
        raises on a non-zero child exit — the code IS the signal. May raise on
        timeout or OS-level spawn failure already surfaced at spawn time.
        A second call returns the cached result and ignores ``timeout``.
        """
        ...

    def poll(self) -> int | None:
        """Liveness probe: exit code if the child has exited, else ``None``.

        Does **not** drain stdout/stderr. Safe only for pid/liveness checks while
        a concurrent :meth:`wait` is already draining, or when child output is
        known-bounded. A poll-only loop against a verbose child deadlocks once
        the OS pipe buffer fills — see the module pipe-drain contract.
        """
        ...

    def terminate(self) -> None:
        """Ask the child to exit (``SIGTERM`` / Windows terminate)."""
        ...


class Runner(Protocol):
    """The injected effect boundary — the only thing that actually touches the host.

    Tests pass a fake; production passes :class:`SubprocessRunner`. Keeping this the sole
    side-effect surface is what lets every render/plan path stay pure and offline.

    ``spawn`` is the primitive (pid now, wait later). ``run`` is the blocking
    convenience — implementations must define ``spawn``; :class:`RunnerBase`
    supplies ``run`` as ``spawn(...).wait()``.
    """

    def spawn(
        self, argv: list[str], *, input: str | None = None, cwd: Path | None = None
    ) -> ProcessHandle:
        """Start a child; return a handle whose pid is already observable.

        Production implementations PIPE both stdout and stderr. Streams are
        drained **only** by :meth:`ProcessHandle.wait` — not by
        :meth:`ProcessHandle.poll`. After spawn, either call ``wait()`` promptly
        (or inherit it via :meth:`run`), or use the factory drain pattern: a pool
        thread blocks in ``wait()`` while other code reads pid/liveness. A
        poll-without-wait loop deadlocks once the child fills the pipe buffer.
        """
        ...

    def run(
        self, argv: list[str], *, input: str | None = None, cwd: Path | None = None
    ) -> RunResult:
        """Blocking convenience: ``spawn(...).wait()``. Prefer implementing spawn once."""
        ...


class RunnerBase:
    """Default ``run()`` over ``spawn().wait()`` — implementers write spawn once.

    Concrete runners (production and test fakes) subclass this and implement
    :meth:`spawn`; they inherit the blocking convenience for free.
    """

    def run(
        self, argv: list[str], *, input: str | None = None, cwd: Path | None = None
    ) -> RunResult:
        return self.spawn(argv, input=input, cwd=cwd).wait()

    def spawn(
        self, argv: list[str], *, input: str | None = None, cwd: Path | None = None
    ) -> ProcessHandle:  # pragma: no cover - abstract for subclasses
        raise NotImplementedError


class _PopenHandle:
    """ProcessHandle over ``subprocess.Popen`` — pid immediate; wait captures streams.

    stdout and stderr are PIPEd at spawn. :meth:`wait` is the sole drain path
    (``Popen.communicate``); :meth:`poll` only checks exit status and never reads
    the pipes. Callers that poll without a concurrent/pending ``wait()`` will
    deadlock if the child writes more than the OS pipe buffer holds — see the
    module pipe-drain contract (factory drain: pool thread waits, other code
    reads pid/liveness).
    """

    def __init__(self, proc: subprocess.Popen[str], input_text: str | None) -> None:
        self._proc = proc
        self._input = input_text
        self._result: RunResult | None = None

    @property
    def pid(self) -> int:
        return self._proc.pid

    def wait(self, timeout: float | None = None) -> RunResult:
        if self._result is not None:
            return self._result
        stdout, stderr = self._proc.communicate(input=self._input, timeout=timeout)
        self._result = RunResult(
            returncode=self._proc.returncode if self._proc.returncode is not None else -1,
            stdout=stdout or "",
            stderr=stderr or "",
        )
        return self._result

    def poll(self) -> int | None:
        # Liveness only — does not drain pipes (see class docstring).
        if self._result is not None:
            return self._result.returncode
        return self._proc.poll()

    def terminate(self) -> None:
        self._proc.terminate()


class SubprocessRunner(RunnerBase):
    """The one production :class:`Runner` — runs a child, capturing stdout/stderr SEPARATELY.

    Built on ``subprocess.Popen`` so :meth:`spawn` can return a handle with an immediate
    pid (FACTORY-PLAN T5). Never raises on a non-zero child: the exit code IS the signal
    callers collect. ``input`` is piped to the child's stdin when given (schedkit's
    ``crontab -``); ``cwd`` scopes the run to a directory when given. Streams stay apart
    so a failure reason (stderr) can be surfaced without the caller's parse depending on
    a clean stdout.

    ``run()`` is inherited from :class:`RunnerBase` (``spawn(...).wait()``) and stays
    byte-identical to the historical blocking capture path: str() argv coercion,
    optional input piping, optional cwd, ``""`` for None streams, never raises on
    nonzero exit.
    """

    def spawn(
        self, argv: list[str], *, input: str | None = None, cwd: Path | None = None
    ) -> ProcessHandle:
        # Both streams PIPEd — only wait() drains them (see module pipe-drain contract).
        proc = subprocess.Popen(
            [str(a) for a in argv],
            stdin=subprocess.PIPE if input is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
            text=True,
        )
        return _PopenHandle(proc, input)
