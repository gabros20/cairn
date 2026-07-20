"""The ``hermes`` executor — Nous Research's Hermes Agent CLI, headless (``-z``/``--oneshot``).

Hermes' one-shot mode (`-z`) prints ONLY the final response text to stdout with approvals
auto-bypassed — purpose-built for cairn's scripted, non-interactive step model. Two shapes set
it apart from the other CLI executors, both driven by the feasibility report
(.orchestrate/xcli-feasibility-hermes.md):

  * Prompt is delivered on ARGV (`-z <prompt>`), not stdin — `-z` has no stdin path (report §2).
  * There is NO per-invocation reasoning-effort flag; effort is config-only. cairn delivers it
    by baking `agent.reasoning_effort` into a per-run clean-room ``HERMES_HOME/config.yaml``
    (see ``_extra_env``), generated only when a tier resolves an effort (report §3).

Like claude, hermes has no OS-level sandbox of its own — `-z` bypasses approvals with zero
confinement underneath — so its process is wrapped in cairn's `fs` OS filesystem sandbox
(report §5 / verdict risk 1; sandbox posture below).
"""

from __future__ import annotations

import os

from cairn.executors._cli import CliExecutor
from cairn.executors.base import Capabilities, Finding, Invocation

# cairn EFFORTS = low|medium|high|xhigh|max (kernel/types.py). Hermes has NO per-invocation
# reasoning-effort CLI flag — it is config-only (`agent.reasoning_effort` in config.yaml; report
# §3 grepped the full argparse surface and found no `--reasoning-effort`/`-e` flag). cairn
# delivers effort by baking it into a per-run HERMES_HOME/config.yaml (see ``_extra_env``).
# Hermes' accepted reasoning_effort vocabulary was observed only as "medium" on the test machine
# and never fully enumerated [UNC]; the safe, documented mapping passes low/medium/high through
# and folds cairn's two higher tiers (xhigh/max — hermes has no analog) down to the nearest value
# it is known to accept, "high". An unrecognized effort maps to the neutral "medium" default
# rather than crashing (brief rule 5: unmappable → nearest value, never a crash).
_EFFORT_MAP = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",  # no hermes xhigh tier — nearest is high [UNC on hermes vocab]
    "max": "high",    # no hermes max tier — nearest is high [UNC on hermes vocab]
}


def _map_effort(effort: str) -> str:
    """cairn effort → hermes ``agent.reasoning_effort`` value. Nearest-value; never raises."""
    return _EFFORT_MAP.get(effort, "medium")


# Report §1 + verdict caveat (d): the newest/most-capable providers auth via OAuth, which needs a
# one-time interactive browser consent — unusable in cairn's cold subprocess context. The only
# headless-viable path is an API-key provider. cairn keeps auth env-side (SECURITY: never on
# disk), so doctor WARNs — never errors — when none of these API-key env vars is present in
# os.environ. Advisory only: a run may still succeed via ~/.hermes/.env if configured.
_API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")


class HermesExecutor(CliExecutor):
    name = "hermes"
    # None, not "AGENTS.md": cairn's doctrine is composed verbatim into the -z prompt envelope by
    # compose.py (_doctrine), and `--ignore-rules` (below) suppresses cwd AGENTS.md auto-injection
    # anyway — so writing an AGENTS.md here would be a lie (hermes would ignore it). Doctrine still
    # reaches the agent, via the prompt, exactly like grok.
    _workspace_file = None
    _install_hint = "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
    capabilities = Capabilities(
        # Headless blocking-hook support exists in source (pre_tool_call can veto a tool call,
        # report §5/§10) but cairn does not wire a hermes hook — leave None so the doctor probe
        # decides, rather than assert a capability cairn never installs (mirrors grok/codex).
        blocking_hooks=None,
        # `-z` has no --json/--output-schema; the STEP sentinel embedded in the final text is the
        # sole contract (report §8).
        output_schema=False,
        # `-z` writes no resumable session_id line and cairn passes no --resume/--continue, so
        # there is nothing for cairn to capture (report §7).
        session_capture=None,
        installs_hooks=False,  # install_guards is a no-op — cairn does not wire a hermes hook
        # C8/W3c + report §5/verdict risk 1: `-z` unconditionally bypasses approvals with NO
        # OS-level confinement of its own, so cairn wraps its process in the OS filesystem sandbox
        # (writes confined to run_dir+workspace) — the same load-bearing posture as claude's
        # bypassPermissions, per [[cairn-bypasspermissions-fs-residual]].
        sandbox="fs",
    )
    # The flags `_build_command` can emit — doctor re-verifies each is still advertised by the
    # installed CLI's `hermes --help` (W5b sub-change A.1). All are top-level `hermes --help`
    # flags (report §2/§5/§6), not subcommand flags, so the default `_help_argv` covers them.
    # `--ignore-user-config` is conditional (effort-None branch only) but still listed here — the
    # drift check verifies the flag EXISTS, not that every argv emits it.
    _emitted_flags: tuple[str, ...] = (
        "-z", "-m", "--ignore-user-config", "--ignore-rules",
        "--accept-hooks", "--yolo", "--usage-file",
    )
    # NOTE (W5b sub-change A.2): hermes DOES have a fetchable model-catalog JSON endpoint (report
    # §10, config.yaml's `model_catalog.url`), but cairn's doctor does NOT poll it — that would add
    # a network dependency to an otherwise offline, per-machine doctor probe. No `_model_findings`
    # override (inherits the no-op default): a bad `-m` slug errors loudly at run time instead,
    # same posture as codex.

    def _extra_env(self, inv: Invocation) -> dict[str, str] | None:
        # Effort has no CLI flag (report §3) — its only channel is config.yaml under the hermes
        # home. When a tier resolves an effort, point HERMES_HOME at a per-INVOCATION CLEAN-ROOM
        # home (under the run dir, brief rule 3) carrying ONLY that setting; when effort is None
        # there is nothing to deliver, so no home is generated — the default ~/.hermes is used,
        # sealed by `--ignore-user-config` in _build_command instead. CI hygiene (report §11):
        # hermes has no telemetry/auto-update env to disable, so this hook adds ONLY HERMES_HOME.
        #
        # Keyed on inv.log_path.stem (unique per step+attempt+cycle, walk.py's _log_path), NOT on
        # inv.cwd alone: inv.cwd is the run dir SHARED by every step in a run (walk.py sets
        # cwd=self.run_dir unconditionally), and a ParallelNode runs its children concurrently on
        # the SAME executor instance. Deriving the home from inv.cwd alone let two parallel hermes
        # steps with different effort race to write the same config.yaml — one step's effort would
        # silently win for BOTH (xcli-review-quality.md H1). Keying per-invocation also means a
        # retried attempt gets a FRESH home rather than reusing another attempt's stale state.
        if inv.effort is None:
            return None
        home = inv.cwd / ".cairn" / f"hermes-home.{inv.log_path.stem}"
        home.mkdir(parents=True, exist_ok=True)
        # Minimal config — reasoning_effort only, hand-written (deterministic, no yaml import
        # needed for two lines). NEVER a secret here (auth stays env-side, brief rule 4).
        # Idempotent: identical runs write byte-identical content.
        # L1 (xcli-review-quality.md): unlike kimi's _toml_str, this emitter needs no
        # escaping — _map_effort's return value is ALWAYS one of the three literal strings
        # "low"/"medium"/"high" (the _EFFORT_MAP table or its "medium" fallback), never a
        # pass-through of an arbitrary operator/agent-controlled string, so there is no value
        # here that could ever carry a quote/newline/control char into the YAML.
        config = home / "config.yaml"
        body = f"agent:\n  reasoning_effort: {_map_effort(inv.effort)}\n"
        if not config.exists() or config.read_text(encoding="utf-8") != body:
            config.write_text(body, encoding="utf-8")
        return {"HERMES_HOME": str(home)}

    def _build_command(self, inv: Invocation, prompt_text: str) -> tuple[list[str], str | None]:
        # re-verify against `hermes --help` at doctor time; vendors drift. Facts below are from
        # the feasibility report's LIVE `hermes --help` capture (hermes v0.18.2), tags inline.
        usage_path = inv.cwd / ".cairn" / "hermes-usage.json"
        usage_path.parent.mkdir(parents=True, exist_ok=True)
        argv = [
            "hermes",
            # -z/--oneshot [LIVE, report §2]: send one prompt, print ONLY the final response text
            # to stdout (no banner/spinner/session line) — nothing pollutes the trailing
            # <<<STEP{...}>>> sentinel — and its docstring states "approvals are auto-bypassed",
            # so a headless step is never gated on an interactive approval. Prompt rides ARGV:
            # `-z` has no documented stdin path (report §2), unlike claude/codex.
            "-z", prompt_text,
            # -m/--model [LIVE, report §3]: pin the model per-invocation. cairn's tier model is a
            # provider-qualified slug (e.g. "anthropic/claude-opus-4.8"), so the provider is
            # carried by the slug and a separate --provider is NOT emitted (the report proposed
            # --provider; dropped — cairn has no provider field, and deriving it from the slug
            # prefix would be fragile). Always pass -m explicitly: a shared home's configured
            # default can silently drift between runs (report §3 / verdict risk 3).
            "-m", inv.model,
        ]
        if inv.effort is None:
            # --ignore-user-config [LIVE, report §6]: seal ~/.hermes/config.yaml → built-in
            # defaults (credentials in .env still load) for deterministic runs. Emitted ONLY when
            # there is no per-run HERMES_HOME to protect: when effort IS set, _extra_env points
            # HERMES_HOME at a clean-room home whose config.yaml carries the effort, and
            # --ignore-user-config would ALSO ignore THAT relocated config.yaml (HERMES_HOME
            # relocates the whole ~/.hermes, so its config.yaml IS "the user config"), silently
            # dropping the effort. So it is omitted in the effort branch, where the clean-room home
            # (no user data to leak) is the isolation instead. [UNC: --ignore-user-config's exact
            # scope vs a relocated HERMES_HOME was not live-verified — this is the safe reading.]
            argv += ["--ignore-user-config"]
        argv += [
            # --ignore-rules [LIVE, report §6]: skip auto-injection of
            # AGENTS.md/SOUL.md/.cursorrules/memory/skills from cwd, so identical runs are
            # deterministic. Doctrine still reaches the agent — compose.py folds it verbatim into
            # the -z prompt (which is why _workspace_file is None above).
            "--ignore-rules",
            # --accept-hooks [LIVE, report §5/§10]: auto-consent any pre-registered shell hooks in
            # this HERMES_HOME without a TTY prompt — a no-op when none are configured (the
            # clean-room/default home has none), but required so a declared blocking hook is not
            # silently skipped headlessly.
            "--accept-hooks",
            # --yolo [LIVE, report §5]: belt-and-suspenders alongside -z's own auto-bypass —
            # bypass dangerous-command approval prompts, in case a future hermes narrows -z's
            # unconditional bypass scope.
            "--yolo",
            # --usage-file [LIVE, report §2]: write a JSON usage report (cost/tokens/model), even
            # on failure. Purely an observability sidecar under the run dir — NEVER load-bearing
            # for the Result (the STEP sentinel on stdout is the sole contract); nothing reads it
            # back. Only trivial plumbing (mkdir), so it is emitted per the brief.
            "--usage-file", str(usage_path),
        ]
        return argv, None  # prompt delivered via -z argv; hermes -z reads no stdin (report §2)

    # -- doctor ------------------------------------------------------------- #

    def doctor(self) -> list[Finding]:
        findings = super().doctor()
        # Auth advisory only after the binary/version probe passed — an error there already tells
        # the user to install/fix, and layering an auth WARN on top would be noise.
        if not any(f.level == "error" for f in findings):
            findings += self._auth_findings()
        return findings

    def _auth_findings(self) -> list[Finding]:
        """WARN (never error) when no API-key provider env var is present (report §1 / verdict
        caveat d). Mirrors how the other executors phrase advisory probe warnings."""
        if any(v in os.environ for v in _API_KEY_ENV_VARS):
            return []
        return [
            Finding(
                "warning",
                f"{self.name}: no API-key provider env var found "
                f"(checked {', '.join(_API_KEY_ENV_VARS)}); headless runs need an API-key "
                f"provider — OAuth providers require one-time interactive consent. A run may "
                f"still work via ~/.hermes/.env if configured.",
            )
        ]
