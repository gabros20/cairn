"""The pinned kernel contracts.

This module is imported by every other kernel module and by all executor plugins.
Its signatures are a stability surface: parallel agents implement against exactly
what is declared here, so changes ripple widely. Keep it small and precise.

References: docs/API.md §6 (executor protocol), docs/ARCHITECTURE.md §9 (exit codes),
docs/SECURITY.md §4 (budget exit code).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

# Abstract model tiers (never vendor names — cairn.toml maps tier → concrete model per executor)
# and the shared effort enum. Both are ordered, lowest-cost last where it matters.
TIERS: tuple[str, ...] = ("reasoning", "balanced", "cheap")
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh")


class ExitCode(IntEnum):
    """Process exit codes = the failure taxonomy (docs/ARCHITECTURE.md §9)."""

    OK = 0
    CONFIG = 2       # plan-time config error
    GATE_FAILED = 3  # artifact gate / validator failed
    EXECUTOR = 4     # executor spawn/auth/crash
    TIMEOUT = 5
    NEEDS_HUMAN = 6  # halted at manual/gate in headless mode
    BUDGET = 7       # budget exceeded (docs/SECURITY.md §4)


@dataclass(frozen=True)
class Finding:
    """One diagnostic from doctor / config load. `fix` is a copy-pasteable remedy."""

    level: Literal["error", "warning", "info"]
    message: str
    fix: str | None = None


@dataclass(frozen=True)
class Capabilities:
    """What an executor can actually enforce — declared, never assumed (docs/CONCEPTS.md §6)."""

    blocking_hooks: bool | None  # None = unknown → doctor probes empirically
    output_schema: bool          # native typed-return support (used as a bonus only)
    session_capture: str | None  # glob of session files to copy into logs/, if any


@dataclass(frozen=True)
class Invocation:
    """Everything one headless CLI process needs. cwd is always the run dir."""

    prompt_file: Path      # the rendered envelope
    model: str             # already tier-resolved
    effort: str | None     # None when baked into a model alias
    cwd: Path              # the run dir
    env: dict[str, str]    # CAIRN_* + guard shims on PATH
    timeout_s: int
    log_path: Path
    return_schema: Path
    # Literal-value scrubber (SECURITY.md §1.3) threaded into the executor's log-write path,
    # so declared secrets are redacted from logs/<step>.log line-by-line as they stream. None
    # (no secret resolved) ⇒ the log is teed verbatim, byte-for-byte as before.
    redactor: Callable[[str], str] | None = None


@dataclass(frozen=True)
class Result:
    """The outcome of one invocation. `step` is the parsed STEP block, None if unparsable."""

    step: dict | None
    exit_code: int
    duration_s: float
    # Executor-reported token/cost usage, when available. All three executors run with
    # plain-text output today, so this is None — the future source is a per-CLI json
    # output-format (e.g. `--output-format json`), which the walker will prefer over any
    # model-self-reported STEP-block `usage`. The stable schema is the value now, not numbers.
    usage: dict | None = None


@runtime_checkable
class Executor(Protocol):
    """The CLI binding — the only CLI-aware code in the system (docs/API.md §6).

    Five operations. Implementations register via the ``cairn.executors`` entry point.

    ``install_guards`` / ``render_workspace`` take the workspace object and the parsed
    guard list; those concrete types are owned by sibling kernel modules that other
    agents build (guards.py, plan.py), so they are left unannotated here to keep this
    pinned contract from coupling to not-yet-existing modules.
    """

    name: str
    capabilities: Capabilities

    def doctor(self) -> list[Finding]:
        """Preflight: auth, version, hook-firing probe."""
        ...

    def resolve_model(self, tier: str, effort: str) -> tuple[str, str | None]:
        """tier + effort → (concrete model id, effort or None if baked into the model)."""
        ...

    def invoke(self, inv: Invocation) -> Result:
        """Run ONE subprocess, blocking, and return its Result."""
        ...

    def install_guards(self, guards, workspace) -> None:
        """Wire native pre-tool hooks / PATH shims for this executor. Idempotent."""
        ...

    def render_workspace(self, workspace) -> None:
        """Emit any per-CLI workspace files (AGENTS.md etc.). Idempotent."""
        ...
