"""The kernel error hierarchy.

Errors carry structured detail (findings, reasons, source location) so the CLI can
render precise diagnostics and retry envelopes can feed reasons back verbatim.
"""

from __future__ import annotations

from cairn.kernel.types import Finding


class CairnError(Exception):
    """Base for every error the kernel raises deliberately."""


class ConfigError(CairnError):
    """A plan-time configuration problem (exit code 2).

    Carries the structured `findings` behind the failure plus optional source
    location so the operator is pointed at the exact workspace file and line.
    """

    def __init__(
        self,
        message: str,
        *,
        findings: list[Finding] | None = None,
        file: str | None = None,
        line: int | None = None,
    ) -> None:
        super().__init__(message)
        self.findings: list[Finding] = list(findings) if findings else []
        self.file = file
        self.line = line


class ExecutorSpawnError(CairnError):
    """A subprocess failed to spawn (exit code 4): missing/non-executable binary, bad cwd, …

    Carries the resolved executable name so the operator can act on it. Never carries the
    full ``argv``/``env`` (SECURITY §1.3) — either can hold secrets — so the message is
    limited to the executable name plus the OS diagnostic (errno/strerror).
    """

    def __init__(self, message: str, *, executable: str | None = None) -> None:
        super().__init__(message)
        self.executable = executable


class ValidationFailure(CairnError):
    """An artifact failed its schema/validator (exit code 3).

    `reasons` are the machine-readable lines a validator emitted; they are fed into
    the halt message, the trail, and retry envelopes verbatim.
    """

    def __init__(self, message: str, *, reasons: list[str] | None = None) -> None:
        super().__init__(message)
        self.reasons: list[str] = list(reasons) if reasons else []
