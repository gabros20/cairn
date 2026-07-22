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
from enum import Enum, IntEnum
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

# Abstract model tiers (never vendor names — cairn.toml maps tier → concrete model per executor)
# and the shared effort enum. Both are ordered, lowest-cost last where it matters.
TIERS: tuple[str, ...] = ("reasoning", "balanced", "cheap")
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


class ExitCode(IntEnum):
    """Process exit codes = the failure taxonomy (docs/ARCHITECTURE.md §9)."""

    OK = 0
    CONFIG = 2       # plan-time config error
    GATE_FAILED = 3  # artifact gate / validator failed
    EXECUTOR = 4     # executor spawn/auth/crash
    TIMEOUT = 5
    NEEDS_HUMAN = 6  # halted at manual/gate in headless mode
    BUDGET = 7       # budget exceeded (docs/SECURITY.md §4)
    CAPACITY = 8     # halted waiting for an agent slot — resumable
    BLOCKED = 9      # halted on auth/network/environment trouble — needs an operator, resumable


class OutcomeClass(str, Enum):
    """High-level run outcome class (doctrine D8 — docs/FACTORY-PLAN.md)."""

    DONE = "done"
    WAITING = "waiting"
    FAILED = "failed"


@dataclass(frozen=True)
class RunOutcome:
    """Classified process outcome (doctrine D8 — docs/FACTORY-PLAN.md).

    Waiting-class outcomes carry a ``waiting_kind``; DONE and FAILED leave it None.
    """

    cls: OutcomeClass
    waiting_kind: Literal["needs_human", "capacity", "blocked"] | None = None


def classify_exit(code: int) -> RunOutcome:
    """Map a process exit code to a RunOutcome (doctrine D8 — docs/FACTORY-PLAN.md).

    0 → DONE; 6/8/9 → WAITING (needs_human/capacity/blocked); any other nonzero
    (known failure codes, unknown positives, signal deaths) → FAILED. Fail-closed:
    unknown codes never look like waiting.
    """
    if code == ExitCode.OK:
        return RunOutcome(cls=OutcomeClass.DONE)
    if code == ExitCode.NEEDS_HUMAN:
        return RunOutcome(cls=OutcomeClass.WAITING, waiting_kind="needs_human")
    if code == ExitCode.CAPACITY:
        return RunOutcome(cls=OutcomeClass.WAITING, waiting_kind="capacity")
    if code == ExitCode.BLOCKED:
        return RunOutcome(cls=OutcomeClass.WAITING, waiting_kind="blocked")
    return RunOutcome(cls=OutcomeClass.FAILED)


@dataclass(frozen=True)
class Finding:
    """One diagnostic from doctor / config load. `fix` is a copy-pasteable remedy."""

    level: Literal["error", "warning", "info"]
    message: str
    fix: str | None = None


@dataclass(frozen=True)
class Capabilities:
    """What an executor can actually enforce — declared, never assumed (docs/CONCEPTS.md §6).

    ``blocking_hooks`` and ``installs_hooks`` answer two DIFFERENT questions and must not be
    conflated: ``blocking_hooks`` is the CLI-capability/probe question — "can this vendor's CLI
    block a tool call via a native pre-execution hook, at all" — an asserted design claim
    (``True``) or an open question the doctor hook-probe settles empirically (``None``/``False``).
    ``installs_hooks`` is an IMPLEMENTATION fact — "does cairn's own ``install_guards`` for this
    executor actually wire that hook for a run" — independent of whether the CLI *could*. An
    executor can have ``blocking_hooks=True`` (the CLI supports it) while ``installs_hooks=False``
    (cairn hasn't wired it yet, e.g. codex/grok today) — asserting ``blocking_hooks=True`` there
    is fine; it is `installs_hooks` that must not overstate what cairn itself does.
    """

    blocking_hooks: bool | None  # None = unknown → doctor probes empirically
    output_schema: bool          # native typed-return support (used as a bonus only)
    session_capture: str | None  # glob of session files to copy into logs/, if any
    # cairn's install_guards wires a real pre-execution blocking hook for this executor (True
    # only for claude, post-W3a) — no default: every executor must state this explicitly, the
    # honesty flag plan.py's effective-layer warning (§4) keys off. codex/grok may assert
    # blocking_hooks independently of this but do NOT install, so a hook-only guard under them
    # is not pre-execution-enforced.
    installs_hooks: bool
    # The OS filesystem-sandbox posture cairn wraps this executor's process in (C8/W3c,
    # cairn.kernel.sandbox). ``off`` = no wrap, argv unchanged (codex/grok self-sandbox via their
    # own ``--sandbox``; shell/stub are trusted). ``fs`` = confine writes to run_dir+workspace,
    # gatekeys dir read-only (claude — the residual close). ``strict`` = fs + network egress control
    # (deferred srt tier). Defaulted so every existing ``Capabilities(...)`` stays valid; only
    # claude sets it non-default today.
    sandbox: str = "off"


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
    # The step's resolved network policy (AgentSpec.network / StepNode.network — plan.py). This
    # was parsed since plan.py but never reached an executor until W5b (codex-F5) added this
    # field. Default false matches every executor's behavior before W5b. Consumed by codex
    # today (`-c sandbox_workspace_write.network_access=true|false`, ARCHITECTURE §5); grok's
    # `--sandbox <PROFILE>` is a single filesystem+network profile with no separate toggle to
    # verify against the captured help, and claude's CLI has no analogous flag — wiring either
    # is future work, NOT a silent drop: this field still carries the resolved value for both,
    # the executor is just not yet honoring it.
    network: bool = False
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

    def install_guards(self, guards, workspace, run_dir) -> None:
        """Wire native pre-tool hooks / PATH shims for this executor. Idempotent.

        ``run_dir`` is the per-run directory (the executor's cwd) — claude reads project hook
        settings from ``<run_dir>/.claude/settings.json``, so a hook-installing executor needs
        it to know where to write."""
        ...

    def render_workspace(self, workspace) -> None:
        """Emit any per-CLI workspace files (AGENTS.md etc.). Idempotent."""
        ...
