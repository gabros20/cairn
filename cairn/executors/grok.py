"""The ``grok`` executor — xAI Grok CLI, headless (single-turn ``--prompt-file`` mode).

grok 0.2.82 has a native headless ``--effort <low|medium|high|xhigh|max>`` flag that covers
cairn's EFFORTS exactly, so tier-resolved effort flows through like the other executors.
"""

from __future__ import annotations

import re

from cairn.executors._cli import CliExecutor, _probe
from cairn.executors.base import Capabilities, Finding, Invocation
from cairn.kernel.errors import CairnError


# `grok models` prints e.g.:
#   Default model: grok-4.5
#
#   Available models:
#     * grok-4.5 (default)
#     - grok-composer-2.5-fast
# One model slug per "  * " / "  - " bulleted line; strip the leading marker and any trailing
# "(default)" annotation (the `\S+` capture already stops before the space that precedes it).
_GROK_MODEL_LINE_RE = re.compile(r"^\s*[-*]\s+(\S+)", re.MULTILINE)


def _parse_grok_models(help_text: str) -> set[str]:
    return {m.group(1) for m in _GROK_MODEL_LINE_RE.finditer(help_text)}


class GrokExecutor(CliExecutor):
    name = "grok"
    _workspace_file = None  # grok reads CLAUDE.md AND AGENTS.md; it writes neither
    _install_hint = "install the grok CLI and run `grok login`"
    capabilities = Capabilities(
        # grok 0.2.82 ships documented blocking PreToolUse hooks (~/.grok/docs/user-guide/
        # 10-hooks.md): JSON files in ~/.grok/hooks/*.json (global, always trusted) or
        # <project>/.grok/hooks/*.json (requires folder trust — `--trust` / /hooks-trust).
        # A hook denies a tool call via stdout {"decision":"deny","reason":...} (honored
        # regardless of exit code) or exit 2; every other failure is fail-open. BUT this is a
        # CLI-capability fact only — cairn's own install_guards below does not wire it (see
        # installs_hooks), so leave None: unknown/unasserted → the doctor hook-probe decides,
        # rather than assert True for a mechanism cairn never installs (grok-F3).
        blocking_hooks=None,
        # grok has native --json-schema (implies --output-format json), but it is NOT
        # wired: native schema is a later bonus; the STEP sentinel is the contract. W5b
        # sub-change B: this previously asserted True, overstating what cairn actually uses
        # (grok-F2).
        output_schema=False,
        session_capture=None,
        installs_hooks=False,  # install_guards is a no-op — cairn does not wire a grok hook yet
    )
    # The flags `_build_command` emits — doctor re-verifies these against the installed CLI's
    # `grok --help` (W5b sub-change A.1). `--no-auto-update` is DELIBERATELY excluded: the
    # captured help (and grok 0.2.82 live) hides it from `--help` while still accepting it (see
    # the comment on the flag below) — asserting it here would WARN on every healthy install.
    _emitted_flags: tuple[str, ...] = (
        "--prompt-file", "--cwd", "-m", "--output-format", "--permission-mode",
        "--no-alt-screen", "--no-memory", "--sandbox", "--effort",
    )

    def _model_findings(self) -> list[Finding]:
        # W5b sub-change A.2: grok is the one executor with a queryable model roster —
        # `grok models` (captured help: "List available models and exit"). Best-effort: a
        # fetch failure or an unparseable roster is one warning, never a crash; the goal is
        # catching model-slug drift before the first paid run, not gatekeeping.
        try:
            code, out = _probe(["grok", "models"])
        except (OSError, CairnError):
            code, out = None, ""
        if code is None or code != 0 or not out:
            return [Finding("warning", "could not run `grok models` — skipping model drift check")]
        known = _parse_grok_models(out)
        if not known:
            return [
                Finding("warning", "`grok models` returned no parseable model list — skipping model drift check")
            ]
        findings = []
        for tier in sorted(self.config.tiers):
            model = self.config.tiers[tier].model
            if model not in known:
                findings.append(
                    Finding(
                        "warning",
                        f"grok tier {tier!r} model {model!r} not in `grok models` "
                        f"(known: {', '.join(sorted(known))})",
                    )
                )
        return findings

    def _build_command(self, inv: Invocation, prompt_text: str) -> tuple[list[str], str | None]:
        # re-verify against `grok --help` at doctor time; vendors drift.
        # Live-verified against grok 0.2.82 (6d0b07d2de0f) at the original build; the W4 additions
        # below (--no-memory, --sandbox, and the --effort/--reasoning-effort alias) were
        # separately live-verified against 0.2.101, the version installed when W4 landed — the
        # two version numbers below are not a typo, just two verification passes over time:
        #   * Headless mode does NOT read the prompt from stdin — bare `-p` is an argv
        #     error ("a value is required for '--single <PROMPT>'") and the headless docs
        #     state piped stdin is never read into the prompt. The envelope can be multi-KB
        #     with newlines/quotes, so deliver it via --prompt-file pointing at the
        #     walker-rendered envelope (inv.prompt_file) instead of a giant argv arg.
        #   * `--output-format text` is an invalid-value error; valid values are
        #     plain|json|streaming-json → plain.
        #   * `--permission-mode dontAsk` silently denies file writes headlessly (exit 0,
        #     empty output, NO artifact — reproduced with a clean GROK_HOME); per the
        #     vendor docs only bypassPermissions is wired via this flag today, and it was
        #     the only mode that let the agent write its artifact. Explicit --deny rules
        #     and PreToolUse hooks still apply under bypassPermissions.
        #   * `--no-auto-update` is hidden from --help on 0.2.82 but still accepted
        #     (unknown flags are rejected at parse time, this one is not) and remains
        #     documented for headless use — keep it so runs never trigger update checks.
        argv = [
            "grok",
            "--prompt-file", str(inv.prompt_file),
            "--cwd", str(inv.cwd),
            "-m", inv.model,
            "--output-format", "plain",
            "--permission-mode", "bypassPermissions",
            "--no-alt-screen",
            "--no-auto-update",
            # W4 config isolation (grok-F6): disable cross-session memory so identical pipeline
            # runs are deterministic (captured help: "Disable cross-session memory for this
            # session").
            "--no-memory",
            # W4 (grok-F5): `--sandbox <PROFILE>` — the CLI help does not enumerate profile names,
            # but the installed CLI ships ~/.grok/docs/user-guide/18-sandbox.md documenting the
            # built-in profiles, and `workspace` is live-verified on grok 0.2.101: `grok --sandbox
            # workspace inspect` applies cleanly (no warning), while a bogus profile name is
            # refused ("could not apply … sandbox profile; refusing to start"). `workspace` reads
            # everywhere and writes only CWD + ~/.grok/ + temp dirs — the workspace-write
            # equivalent to codex's `--sandbox workspace-write`, and cwd here is always the run
            # dir.
            # NOTE (W5-doctor-drift): this profile name is a hard, ungated argv literal — nothing
            # checks it against the installed CLI's actual profile roster, so a future grok
            # renaming/removing "workspace" would hard-fail every grok step (fails closed, but
            # with no earlier warning). Tracked for W5: doctor should verify emitted flags/profile
            # names against the installed CLI, not just assert the binary runs.
            "--sandbox", "workspace",
        ]
        if inv.effort is not None:
            # Native effort: `--effort` IS `--reasoning-effort` — the captured help lists them as
            # the same flag ("--reasoning-effort <EFFORT> ... [aliases: --effort]"), confirmed
            # live on the installed 0.2.101. It takes low|medium|high|xhigh|max — matches cairn's
            # EFFORTS exactly (W4 added "max") — and IS forwarded to the model, not a dead knob.
            # Whether it changes anything is per-model (models_cache.json's
            # `supports_reasoning_effort`; e.g. grok-4.5 reports true, grok-composer-2.5-fast
            # reports false today) — cairn's job is to pass the tier-resolved effort through, not
            # to second-guess per-model support, so the flag is always emitted when effort is set.
            argv += ["--effort", inv.effort]
        return argv, None  # prompt delivered via --prompt-file; stdin is not read headlessly
