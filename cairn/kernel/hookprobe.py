"""The empirical hook-firing probe (``cairn doctor --probe-hooks``, C4).

``ClaudeExecutor.capabilities.blocking_hooks = True`` is an *asserted* design claim: the
whole guard/containment story (docs/ARCHITECTURE.md §4) assumes a native ``PreToolUse`` hook
**still fires AND still blocks** even when ``claude`` runs headless under
``--permission-mode bypassPermissions`` (which it must — the default mode refuses every tool
use). ``bypassPermissions`` skips the *interactive* permission prompt; the open risk is
whether it also silently disables hooks. This module settles that per machine, empirically.

The mechanic, per executor (a :class:`HookRecipe`):

1. Build a throwaway canary project dir (tempfile, always cleaned up).
2. Install that executor's *native* pre-tool hook, configured to (a) write a **marker** file
   proving it FIRED and (b) **deny/block** the tool call via the executor's documented deny
   mechanism (verified against the installed CLI — see each recipe).
3. Invoke the vendor CLI headless with the SAME argv posture the executor uses (for claude
   that includes ``--permission-mode bypassPermissions``), cheapest model, tiny prompt telling
   it to run one harmless command whose side effect is a **sidecar** file.
4. Classify from the two files (never from the CLI's own words):

   ===============  ==============  ===================================================
   marker           sidecar         outcome
   ===============  ==============  ===================================================
   present          absent          ``fires_blocks``   — hook-primary posture viable
   present          present         ``fires_no_block`` — hook fired but the tool ran anyway
   absent           present         ``no_fire``        — hook never ran; the tool ran unguarded
   absent           absent          ``inconclusive``   — the agent attempted no guarded tool
   ===============  ==============  ===================================================

CLI missing / timeout / auth failure is also ``inconclusive`` (a probe of a different reality,
not a verdict). Results are **never cached** — a probe run is a fresh fact each time, and it
only runs under the explicit ``--probe-hooks`` flag, so the token cost is opt-in.

Stdlib + pinned kernel/executor-base modules only.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

# run_process / ExecTimeout are the stable executor-author surface (base.__all__); the probe
# reuses them so a canary invocation runs through exactly the same subprocess machinery a real
# step does (streamed log, group-kill on timeout, EXACT env — never os.environ).
from cairn.executors.base import ExecTimeout, run_process
from cairn.kernel.guards import deny_json

Outcome = Literal["fires_blocks", "fires_no_block", "no_fire", "no_mechanism", "inconclusive"]

# Deterministic canary layout, shared by the engine (which checks the files) and every recipe's
# hook command / prompt (which create them). Kept relative so a fake CLI in tests can reconstruct
# them from its cwd.
_PROBE_SUBDIR = ".cairn-probe"
_MARKER_REL = f"{_PROBE_SUBDIR}/marker"
_SIDECAR_REL = f"{_PROBE_SUBDIR}/sidecar"

# The deny payload written to stdout by claude/codex PreToolUse hooks. Verified against the
# INSTALLED binaries (claude 2.1.199, codex-cli 0.142.5): both honor a `hookSpecificOutput`
# object with `permissionDecision: "deny"` for PreToolUse (grep of each binary's embedded hook
# schema — PreToolUsePermissionDecisionWire = {allow, deny, ask}; deny needs a non-empty reason).
# ONE builder (kernel.guards.deny_json) shared with the production `--hook-check` entry so the
# probe canary and the real hook can never drift.
_DENY_JSON = deny_json("cairn hook probe: blocked by design")

# grok's PreToolUse deny shape is DIFFERENT from claude/codex: a top-level
# ``{"decision": "deny", "reason": ...}`` on stdout, honored REGARDLESS of exit code (grok
# 0.2.82, ~/.grok/docs/user-guide/10-hooks.md → "Output (Blocking Hooks)"). Belt-and-braces: the
# grok hook prints this AND exits 2 (an explicit deny), so blocking is proven even if one path
# regresses.
_GROK_DENY_JSON = json.dumps({"decision": "deny", "reason": "cairn hook probe: blocked by design"})

# The canary GROK_HOME's config.toml: turn OFF every claude/cursor compat cell (grok 0.2.82,
# 05-configuration.md → Harness Compatibility). Without this, grok scans the user's ``~/.claude`` /
# ``~/.cursor`` (they follow $HOME, outside the relocated GROK_HOME) and pulls in their MCP servers,
# permission rules, and hooks — live-observed to stall headless shutdown past the probe timeout and
# to let foreign hooks interfere. Every cell off = a hermetic probe of grok's OWN hook.
#
# ENUMERATION-FRAGILE: compat cells default ON and grok has NO master off-switch — this list only
# covers the sources/cells that exist on 0.2.82. A new ``[compat.<source>]`` vendor or a new cell
# in an existing one silently bypasses it. Re-verify against each new grok version (quick check:
# re-enable one cell, e.g. ``mcps = true``, and observe the headless shutdown stall return).
_GROK_COMPAT_OFF_TOML = (
    "[compat.claude]\nskills = false\nrules = false\nagents = false\nmcps = false\nhooks = false\n\n"
    "[compat.cursor]\nskills = false\nrules = false\nagents = false\nmcps = false\nhooks = false\n"
)

# Passthrough env keys — MIRRORS cairn/kernel/walk.py::_Walk._build_env (the deny-by-default
# baseline every real agent step gets). Kept in lockstep so the probe measures the same reality a
# run experiences: USER/LOGNAME are load-bearing (macOS Keychain OAuth lookup), TMPDIR/HOME/LANG
# shape tool behaviour, PATH resolves the vendor CLI. If walk.py's set changes, change this too.
_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME", "XDG_STATE_HOME")

# Substrings that mark a CLI's own auth/login failure → inconclusive, not a hook verdict.
_AUTH_SIGNS = (
    "not logged in",
    "please run",
    "/login",
    "invalid api key",
    "unauthorized",
    "authentication",
    "no credentials",
    "log in to",
)


@dataclass(frozen=True)
class ProbeResult:
    """One executor's probe outcome. ``detail`` is a human sentence; ``posture`` and
    ``mechanism`` are copied from the recipe for the doctor line + the record. ``warnings``
    carries side-channel findings (e.g. a canary dir that survived cleanup) that the doctor
    prints as warning lines — they never change the outcome or the exit code."""

    executor: str
    outcome: Outcome
    detail: str
    cli_version: str | None
    posture: str      # e.g. "under bypassPermissions"
    mechanism: str    # e.g. "PreToolUse deny-JSON"
    warnings: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Recipes — one per executor. The engine below touches NONE of an executor's
# specifics; a new executor (grok, C5) ships a recipe and slots in unchanged.
# --------------------------------------------------------------------------- #


class HookRecipe:
    """The per-executor probe surface. Subclass and set the class attributes + implement
    ``write_canary`` / ``build_invocation`` / ``build_prompt``; the engine does the rest.

    A recipe for a CLI with **no** native blocking hook mechanism instead sets
    ``static_outcome`` (e.g. ``"no_mechanism"``) and the engine reports it without spending a
    token — that IS the shim-primary answer, not a cop-out."""

    name: str = ""
    model: str = ""             # cheapest model for the canary invocation
    posture: str = ""           # headless-posture note for the report line
    mechanism: str = ""         # how the hook blocks (documented, verified)
    static_outcome: Outcome | None = None
    static_detail: str = ""

    def available(self, env: dict[str, str]) -> bool:
        return shutil.which(self.name, path=env.get("PATH")) is not None

    def version(self, env: dict[str, str]) -> str | None:
        """``<name> --version`` under the probe env; None on timeout/failure."""
        with tempfile.TemporaryDirectory() as td:
            try:
                code, out, _ = run_process(
                    [self.name, "--version"],
                    stdin_text=None,
                    env=env,
                    cwd=Path(td),
                    timeout_s=15.0,
                    log_path=Path(td) / "v.log",
                )
            except ExecTimeout:
                return None
            return out.strip() if code == 0 else None

    def extra_env(self, canary: Path) -> dict[str, str]:
        """Executor-specific env additions (e.g. codex CODEX_HOME). Empty by default."""
        return {}

    def write_canary(self, canary: Path, marker: Path, sidecar: Path) -> None:
        raise NotImplementedError

    def build_invocation(
        self, canary: Path, prompt_text: str, model: str
    ) -> tuple[list[str], str | None]:
        """Return (argv, stdin_text) — the SAME headless posture the real executor uses."""
        raise NotImplementedError

    def build_prompt(self, sidecar: Path) -> str:
        raise NotImplementedError

    # -- shared hook-command builder ---------------------------------------- #

    @staticmethod
    def _deny_hook_command(marker: Path) -> str:
        """A ``sh`` one-liner (both CLIs run a hook ``command`` through a shell): create the
        marker proving the hook FIRED, then print the deny-JSON so the tool is BLOCKED."""
        return f": > {shlex.quote(str(marker))}; printf '%s' {shlex.quote(_DENY_JSON)}"

    # -- shared auth seam (codex + grok) ------------------------------------- #

    @staticmethod
    def _real_home(env_var: str, default_subdir: str) -> Path:
        """The user's actual vendor home (``$<env_var>`` or ``~/<default_subdir>``) — read for
        ``auth.json`` ONLY (source stays read-only; isolation is the point)."""
        override = os.environ.get(env_var)
        return Path(override) if override else Path(os.path.expanduser(f"~/{default_subdir}"))

    @staticmethod
    def _copy_auth_into(real_home: Path, canary_home: Path) -> None:
        """Fail-safe auth seam, ONE implementation (security-sensitive lockstep — perms and
        failure handling are fixed here for every recipe): copy ``auth.json`` ALONE from the real
        vendor home into the canary home (0600; it dies with the canary's ``rmtree``) so a live
        probe can authenticate. Any copy failure degrades to the unauthenticated path
        (→ inconclusive), NEVER a probe crash."""
        auth = real_home / "auth.json"
        try:
            if auth.is_file():
                shutil.copyfile(auth, canary_home / "auth.json")
                (canary_home / "auth.json").chmod(0o600)
        except OSError:
            pass


class ClaudeHookRecipe(HookRecipe):
    """``claude`` — Anthropic Claude Code. Verified against claude 2.1.199: project settings live
    in ``<cwd>/.claude/settings.json``; a ``PreToolUse`` hook blocks by printing
    ``hookSpecificOutput.permissionDecision = "deny"`` (deny-JSON). Runs under
    ``--permission-mode bypassPermissions`` — the exact, load-bearing posture the executor uses
    (cairn/executors/claude.py) and the whole point of this probe."""

    name = "claude"
    model = "haiku"  # cheapest; the probe never needs reasoning
    posture = "under bypassPermissions"
    mechanism = "PreToolUse deny-JSON (hookSpecificOutput.permissionDecision=deny)"

    def write_canary(self, canary: Path, marker: Path, sidecar: Path) -> None:
        settings_dir = canary / ".claude"
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": self._deny_hook_command(marker)}],
                    }
                ]
            }
        }
        (settings_dir / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")

    def build_invocation(
        self, canary: Path, prompt_text: str, model: str
    ) -> tuple[list[str], str | None]:
        # LOCKSTEP with cairn/executors/claude.py::ClaudeExecutor._build_command (minus effort):
        # prompt on stdin (W4), text output, --setting-sources project + --strict-mcp-config +
        # --no-session-persistence (W4 isolation), and the load-bearing bypassPermissions the
        # probe exists to stress. `--setting-sources project` also means the probe's own
        # write_canary settings.json (the "project" source, canary is cwd) is what fires — same
        # posture a real run gets from install_guards' run-dir settings.json.
        argv = [
            "claude", "-p",
            "--model", model,
            "--output-format", "text",
            "--permission-mode", "bypassPermissions",
            "--setting-sources", "project", "--strict-mcp-config",
            "--no-session-persistence",
        ]
        return argv, prompt_text

    def build_prompt(self, sidecar: Path) -> str:
        return (
            "Use the Bash tool to run exactly this command and nothing else:\n\n"
            f"touch {shlex.quote(str(sidecar))}\n\n"
            "Do not use any other tool. Do not explain. Just run that one command."
        )


class CodexHookRecipe(HookRecipe):
    """``codex`` — OpenAI Codex CLI. Verified against codex-cli 0.142.5: it ships a full
    Claude-style hooks system (``$CODEX_HOME/hooks.json``, events incl. ``PreToolUse``, a
    ``--dangerously-bypass-hook-trust`` flag for automation). A ``PreToolUse`` hook blocks via the
    same ``hookSpecificOutput.permissionDecision = "deny"`` payload (binary's embedded
    ``PreToolUsePermissionDecisionWire``). CODEX_HOME is relocated to the canary so we install the
    canary hook WITHOUT touching the user's real ``~/.codex`` — with one deliberate exception:
    the AUTH MATERIAL. A relocated CODEX_HOME has no ``auth.json``, which would make every live
    probe ``inconclusive`` (not logged in), so when the real CODEX_HOME has an ``auth.json`` we
    copy that ONE file into the canary (source read-only; NEVER hooks.json/config.toml —
    isolation is the point); it dies with the canary's ``rmtree``. Absent auth → inconclusive,
    exactly as before. Trade-off: the probe briefly holds a credential copy inside the
    throwaway tempdir (0600, lifetime = one invocation)."""

    name = "codex"
    # Live-verified on codex-cli 0.142.5 with a ChatGPT account: gpt-5.5 is the ONLY accepted
    # model — every -mini/-codex variant is rejected ("not supported when using Codex with a
    # ChatGPT account"); see tests/live/workspace-codex/cairn.toml. Don't cargo-cult this onto
    # API-key machines expecting a cheaper tier; there, cost is steered by effort, not model.
    model = "gpt-5.5"
    posture = "headless (codex exec)"
    mechanism = "PreToolUse deny-JSON (hookSpecificOutput.permissionDecision=deny)"

    def extra_env(self, canary: Path) -> dict[str, str]:
        return {"CODEX_HOME": str(canary / ".codex")}

    def write_canary(self, canary: Path, marker: Path, sidecar: Path) -> None:
        home = canary / ".codex"
        home.mkdir(parents=True, exist_ok=True)
        hooks = {
            "hooks": {
                "PreToolUse": [
                    {"hooks": [{"type": "command", "command": self._deny_hook_command(marker)}]}
                ]
            }
        }
        (home / "hooks.json").write_text(json.dumps(hooks, indent=2), encoding="utf-8")
        # A present-but-empty config keeps codex from falling back to the user's config.toml.
        (home / "config.toml").write_text("", encoding="utf-8")
        # Auth seam: the shared fail-safe helper (see HookRecipe._copy_auth_into).
        self._copy_auth_into(self._real_home("CODEX_HOME", ".codex"), home)

    def build_invocation(
        self, canary: Path, prompt_text: str, model: str
    ) -> tuple[list[str], str | None]:
        # LOCKSTEP with cairn/executors/codex.py::CodexExecutor._build_command (effort=None,
        # network=False branch) — pinned by test_codex_invocation_mirrors_real_executor_argv;
        # change that executor and this recipe together. Live-verified on codex-cli 0.142.5:
        # `-a/--ask-for-approval` no longer exists on `codex exec` (argv error), and
        # `--skip-git-repo-check` is required (the canary tempdir is not a git repo).
        # `--ephemeral` (W5b) matches the real executor's session-less posture.
        # `--ignore-user-config`/`--ignore-rules` (W4) skip $CODEX_HOME/config.toml + execpolicy
        # .rules — belt-over-suspenders with the empty config.toml write_canary already plants.
        # `-c sandbox_workspace_write.network_access=false` (W5b) mirrors the real executor's
        # default-false network policy — the probe canary never needs network. The one
        # probe-only addition is --dangerously-bypass-hook-trust, so the freshly-written canary
        # hook runs without persisted hook trust. Prompt on stdin.
        argv = [
            "codex", "exec",
            "-C", str(canary),
            "-m", model,
            "--sandbox", "workspace-write",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "-c", "sandbox_workspace_write.network_access=false",
            "--dangerously-bypass-hook-trust",
        ]
        return argv, prompt_text

    def build_prompt(self, sidecar: Path) -> str:
        return (
            "Run exactly this shell command and nothing else:\n\n"
            f"touch {shlex.quote(str(sidecar))}\n\n"
            "Do not run any other command. Do not explain."
        )


class GrokHookRecipe(HookRecipe):
    """``grok`` — xAI Grok CLI. Verified against grok 0.2.82 (~/.grok/docs/user-guide/10-hooks.md).

    Hook install without touching the user's real ``~/.grok``: ``GROK_HOME`` is relocated to the
    canary and the probe hook is installed as a **GLOBAL** hook at
    ``$GROK_HOME/hooks/cairn-probe.json``. Global hooks are ALWAYS trusted, so this sidesteps the
    folder-trust gate that project hooks (``<project>/.grok/hooks``) require (``--trust`` /
    ``/hooks-trust`` / ``GROK_FOLDER_TRUST``) — the probe never has to grant trust. Grok also scans
    compat sources (``~/.claude``, ``~/.cursor``) by default, which live outside GROK_HOME (they
    follow ``$HOME``, which the probe passes through), so the canary ``config.toml`` disables EVERY
    claude/cursor compat cell (``_GROK_COMPAT_OFF_TOML``) — the probe measures grok's OWN hook, not
    whatever the user has under ``~/.claude`` (whose MCP servers also stall headless shutdown).

    Auth seam (mirrors the codex recipe): a relocated GROK_HOME has no ``auth.json`` → every live
    probe would be ``inconclusive`` (not logged in). When the real GROK_HOME (``$GROK_HOME`` first,
    else ``~/.grok``) has an ``auth.json`` we copy that ONE file into the canary (source read-only,
    0600, dies with the canary's ``rmtree``); any copy failure degrades to the unauthenticated path
    (→ inconclusive), NEVER a crash. Live-verified: a GROK_HOME override with only ``auth.json``
    copied authenticates headless.

    Blocking mechanism: ``PreToolUse`` is the ONLY blocking event. The hook writes the marker, prints
    the deny-JSON (``{"decision":"deny",...}`` — honored regardless of exit code), and exits 2 (an
    explicit deny) — belt-and-braces. CONTAINMENT CAVEAT (inherited by the C3 guard engine): every
    OTHER hook failure — timeout (default 5s), crash, malformed output, missing env — FAILS OPEN
    (the tool runs). Only an explicit ``deny`` blocks. Docs state deny rules + PreToolUse hooks still
    apply under ``bypassPermissions`` — the load-bearing headless posture (cairn/executors/grok.py).

    Model: ``grok-composer-2.5-fast`` with ``--effort low`` — the cheapest path. On THIS install only
    ``grok-composer-2.5-fast`` and ``grok-build`` exist (models_cache.json); don't cargo-cult the
    model onto a machine with a different roster."""

    name = "grok"
    model = "grok-composer-2.5-fast"
    effort = "low"  # cheapest; mirrored into --effort to match the executor's effort branch
    posture = "under bypassPermissions"
    mechanism = 'PreToolUse deny-JSON ({"decision":"deny"}) + exit 2'

    # The global hook filename inside $GROK_HOME/hooks/ (deterministic; the fake CLI globs it).
    _HOOK_FILE = "cairn-probe.json"

    def extra_env(self, canary: Path) -> dict[str, str]:
        return {"GROK_HOME": str(canary / ".grok")}

    @staticmethod
    def _grok_deny_hook_command(marker: Path) -> str:
        """A ``sh`` one-liner: create the marker proving the hook FIRED, print grok's deny-JSON,
        then exit 2 (explicit deny) — so blocking is proven even if either path regresses."""
        return (
            f": > {shlex.quote(str(marker))}; "
            f"printf '%s' {shlex.quote(_GROK_DENY_JSON)}; exit 2"
        )

    def write_canary(self, canary: Path, marker: Path, sidecar: Path) -> None:
        home = canary / ".grok"
        (home / "hooks").mkdir(parents=True, exist_ok=True)
        # NO matcher → matches EVERY tool. Live-verified (grok 0.2.82): the composer/cursor agent's
        # shell tool is named ``Shell``, NOT ``Bash``/``run_terminal_command`` — a ``"Bash"`` matcher
        # silently misses it and the probe reads a false ``no_fire``. A catch-all is correct here:
        # the prompt makes the agent use exactly one shell tool, so any-tool = that tool.
        hook = {
            "hooks": {
                "PreToolUse": [
                    {"hooks": [{"type": "command", "command": self._grok_deny_hook_command(marker)}]}
                ]
            }
        }
        (home / "hooks" / self._HOOK_FILE).write_text(json.dumps(hook, indent=2), encoding="utf-8")
        # Disable ALL always-trusted compat sources (claude + cursor). These follow $HOME (outside
        # GROK_HOME), so without this grok loads the user's ~/.claude MCP servers + permission rules
        # into the canary — live-observed to STALL headless shutdown past the probe timeout (→ a
        # false inconclusive), besides letting foreign hooks interfere. Every cell off = hermetic:
        # the probe measures grok's OWN hook and nothing else.
        (home / "config.toml").write_text(_GROK_COMPAT_OFF_TOML, encoding="utf-8")
        # Auth seam: the shared fail-safe helper (see HookRecipe._copy_auth_into).
        self._copy_auth_into(self._real_home("GROK_HOME", ".grok"), home)

    def build_invocation(
        self, canary: Path, prompt_text: str, model: str
    ) -> tuple[list[str], str | None]:
        # LOCKSTEP with cairn/executors/grok.py::GrokExecutor._build_command (effort="low"
        # branch) — pinned by test_grok_invocation_mirrors_real_executor_argv; change that
        # executor and this recipe together. grok 0.2.82 delivers the prompt via --prompt-file
        # (stdin is dead headless), so write it into the canary and reference it; return stdin=None.
        # NO probe-only extra flag: the hook is GLOBAL (always trusted), so unlike codex there is
        # no folder-trust to bypass — recipe argv == executor argv exactly.
        # W4: --no-memory + --sandbox workspace added to match the executor's config-isolation
        # posture (cairn-F5/F6) — also tightens the probe itself, on top of the relocated GROK_HOME.
        #
        # Deliberately duplicates the engine's own prompt.md write (_probe_in materializes the
        # same path/content just before calling this): the recipe stays self-contained — the file
        # it points --prompt-file at is guaranteed to exist with the right content regardless of
        # caller ordering (e.g. a direct build_invocation call in tests).
        prompt_file = canary / _PROBE_SUBDIR / "prompt.md"
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt_text, encoding="utf-8")
        argv = [
            "grok",
            "--prompt-file", str(prompt_file),
            "--cwd", str(canary),
            "-m", model,
            "--output-format", "plain",
            "--permission-mode", "bypassPermissions",
            "--no-alt-screen",
            "--no-auto-update",
            "--no-memory",
            "--sandbox", "workspace",
            "--effort", self.effort,
        ]
        return argv, None

    def build_prompt(self, sidecar: Path) -> str:
        return (
            "Use the terminal to run exactly this shell command and nothing else:\n\n"
            f"touch {shlex.quote(str(sidecar))}\n\n"
            "Do not run any other command. Do not use any other tool. Do not explain."
        )


# The registered recipes. The engine touches NONE of a recipe's specifics: a new executor ships a
# recipe and slots in unchanged (grok, C5, added its "grok" entry with no engine change).
RECIPES: dict[str, HookRecipe] = {
    "claude": ClaudeHookRecipe(),
    "codex": CodexHookRecipe(),
    "grok": GrokHookRecipe(),
}


# --------------------------------------------------------------------------- #
# The engine — executor-agnostic.
# --------------------------------------------------------------------------- #


def build_probe_env(canary: Path, workspace_dir: Path) -> dict[str, str]:
    """The canary invocation's env — the SAME deny-by-default baseline the walker gives a real
    step (``_ENV_PASSTHROUGH`` mirrors walk.py), so the probe measures run-reality, not a
    different one. ``CLAUDE_PROJECT_DIR``/``CAIRN_RUN_DIR`` point at the canary because the canary
    IS the project for this probe."""
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env["CAIRN_RUN_DIR"] = str(canary)
    env["CAIRN_STEP"] = "doctor-hook-probe"
    env["CAIRN_WORKSPACE"] = str(workspace_dir)
    env["CLAUDE_PROJECT_DIR"] = str(canary)
    return env


def _classify(fired: bool, ran: bool) -> tuple[Outcome, str]:
    if fired and not ran:
        return "fires_blocks", "the PreToolUse hook fired and the guarded tool was blocked"
    if fired and ran:
        return "fires_no_block", "the PreToolUse hook fired but the guarded tool still ran"
    if not fired and ran:
        return "no_fire", "the guarded tool ran with NO PreToolUse hook firing"
    return (
        "inconclusive",
        "the agent attempted no guarded tool (marker and side-effect both absent)",
    )


def _looks_like_auth_failure(output: str, code: int) -> bool:
    low = output.lower()
    return code != 0 and any(sign in low for sign in _AUTH_SIGNS)


# How long the cleanup waits before its single retry — long enough for a detached vendor
# child (observed live: codex's plugins-clone writer) to finish its late write, short enough
# not to stall doctor.
_CLEANUP_RETRY_WAIT_S = 1.0


def probe(
    recipe: HookRecipe,
    *,
    workspace_dir: Path,
    timeout_s: int = 120,
    model: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> ProbeResult:
    """Run ``recipe`` once against a fresh canary and return its :class:`ProbeResult`.

    ``extra_env`` is a test seam (merged last, over the walker baseline); the production doctor
    path passes nothing, preserving env fidelity. Never caches. Cleanup removes the canary,
    retrying once after a bounded wait if a detached vendor child re-created files; a canary
    that STILL survives is reported in ``result.warnings`` (it may contain copied auth
    material) — never raised."""
    model = model or recipe.model

    if recipe.static_outcome is not None:
        return ProbeResult(
            recipe.name, recipe.static_outcome, recipe.static_detail, None, recipe.posture, recipe.mechanism
        )

    tmp = Path(tempfile.mkdtemp(prefix="cairn-hookprobe-"))
    try:
        result = _probe_in(
            recipe, tmp, workspace_dir=workspace_dir, timeout_s=timeout_s,
            model=model, extra_env=extra_env,
        )
    finally:
        leftover = _cleanup_canary(tmp)
    if leftover is not None:
        result = replace(
            result,
            warnings=result.warnings + (
                f"canary dir left behind at {leftover} — a detached {recipe.name} child "
                f"re-created files after cleanup; remove it manually (it may contain copied "
                f"auth material)",
            ),
        )
    return result


def _cleanup_canary(tmp: Path) -> Path | None:
    """Remove the canary root; on leftovers, wait briefly and retry ONCE. Returns the path if
    it still exists afterward (the caller turns that into a warning). Never raises."""
    shutil.rmtree(tmp, ignore_errors=True)
    if not tmp.exists():
        return None
    time.sleep(_CLEANUP_RETRY_WAIT_S)
    shutil.rmtree(tmp, ignore_errors=True)
    return tmp if tmp.exists() else None


def _probe_in(
    recipe: HookRecipe,
    tmp: Path,
    *,
    workspace_dir: Path,
    timeout_s: int,
    model: str,
    extra_env: dict[str, str] | None,
) -> ProbeResult:
    """The probe body, against a caller-owned tempdir (the caller cleans up)."""
    canary = tmp / "canary"
    (canary / _PROBE_SUBDIR).mkdir(parents=True)
    marker = canary / _MARKER_REL
    sidecar = canary / _SIDECAR_REL

    env = build_probe_env(canary, workspace_dir)
    env.update(recipe.extra_env(canary))
    if extra_env:
        env.update(extra_env)

    if not recipe.available(env):
        return ProbeResult(
            recipe.name, "inconclusive",
            f"{recipe.name!r} not found on PATH — nothing to probe",
            None, recipe.posture, recipe.mechanism,
        )

    version = recipe.version(env)
    recipe.write_canary(canary, marker, sidecar)
    prompt_text = recipe.build_prompt(sidecar)
    (canary / _PROBE_SUBDIR / "prompt.md").write_text(prompt_text, encoding="utf-8")
    argv, stdin_text = recipe.build_invocation(canary, prompt_text, model)

    try:
        code, output, _dur = run_process(
            argv,
            stdin_text=stdin_text,
            env=env,
            cwd=canary,
            timeout_s=timeout_s,
            log_path=canary / _PROBE_SUBDIR / "invoke.log",
        )
    except ExecTimeout:
        return ProbeResult(
            recipe.name, "inconclusive",
            f"the canary invocation timed out after {timeout_s}s",
            version, recipe.posture, recipe.mechanism,
        )

    if _looks_like_auth_failure(output, code):
        return ProbeResult(
            recipe.name, "inconclusive",
            f"{recipe.name} reported an auth/login failure (run its login) — cannot probe",
            version, recipe.posture, recipe.mechanism,
        )

    outcome, detail = _classify(marker.exists(), sidecar.exists())
    return ProbeResult(recipe.name, outcome, detail, version, recipe.posture, recipe.mechanism)


# --------------------------------------------------------------------------- #
# Doctor rendering + exit policy.
# --------------------------------------------------------------------------- #

_OK = "✔"
_BAD = "✗"

# Which outcomes describe a viable hook-primary posture vs a shim-primary fallback.
_POSTURE = {
    "fires_blocks": "hook-primary",
    "fires_no_block": "shim-primary",
    "no_fire": "shim-primary",
    "no_mechanism": "shim-primary",
    "inconclusive": "shim-primary (unproven)",
}


def render(result: ProbeResult, blocking_hooks: bool | None) -> tuple[str, str]:
    """Map a result + the executor's asserted ``capabilities.blocking_hooks`` to
    ``(level, line)`` for the doctor.

    Exit policy (level ``"error"`` counts toward doctor's non-zero exit):
    - ``blocking_hooks is True`` (an asserted design claim): ``fires_blocks`` → ok; a concrete
      *falsification* (``fires_no_block`` / ``no_fire`` / ``no_mechanism``) → **error**;
      ``inconclusive`` → warning (never an error — a probe of a different reality).
    - ``blocking_hooks is None``/``False`` (unknown — the probe DECIDES posture): any concrete
      outcome is an informational finding; ``inconclusive`` → warning.
    """
    posture = _POSTURE[result.outcome]
    under = f" {result.posture}" if result.posture else ""

    if result.outcome == "fires_blocks":
        msg = f"PreToolUse fires+blocks{under} → {posture}"
    elif result.outcome == "fires_no_block":
        msg = f"PreToolUse fires but does NOT block{under} → {posture}"
    elif result.outcome == "no_fire":
        msg = f"hooks did NOT fire headless{under} → {posture}"
    elif result.outcome == "no_mechanism":
        msg = f"{result.detail or 'no native blocking hook mechanism'} → {posture}"
    else:  # inconclusive
        msg = f"inconclusive: {result.detail}"

    falsified = result.outcome in ("fires_no_block", "no_fire", "no_mechanism")
    if blocking_hooks is True:
        if result.outcome == "fires_blocks":
            level = "ok"
        elif falsified:
            level = "error"
        else:  # inconclusive
            level = "warning"
    else:  # None / False — the probe is deciding, so no concrete outcome is an error
        level = "warning" if result.outcome == "inconclusive" else "info"

    # One space after every mark: "✔ "/"✗ " lead full-width; "  ! "/"  · " carry their own
    # two-space indent inside the mark, matching doctor's warning/info convention.
    mark = {"ok": _OK, "error": _BAD, "warning": "  !", "info": "  ·"}[level]
    return level, f"{mark} hook probe {result.executor}   {msg}"
