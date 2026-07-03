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


class ValidationFailure(CairnError):
    """An artifact failed its schema/validator (exit code 3).

    `reasons` are the machine-readable lines a validator emitted; they are fed into
    the halt message, the trail, and retry envelopes verbatim.
    """

    def __init__(self, message: str, *, reasons: list[str] | None = None) -> None:
        super().__init__(message)
        self.reasons: list[str] = list(reasons) if reasons else []
