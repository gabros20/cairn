"""The ``grok`` executor — xAI Grok CLI, headless (single-turn ``--prompt-file`` mode).

grok 0.2.82 has a native headless ``--effort <low|medium|high|xhigh|max>`` flag that covers
cairn's EFFORTS exactly, so tier-resolved effort flows through like the other executors.
"""

from __future__ import annotations

from cairn.executors._cli import CliExecutor
from cairn.executors.base import Capabilities, Invocation


class GrokExecutor(CliExecutor):
    name = "grok"
    _workspace_file = None  # grok reads CLAUDE.md AND AGENTS.md; it writes neither
    _install_hint = "install the grok CLI and run `grok login`"
    capabilities = Capabilities(
        # grok 0.2.82 ships documented blocking PreToolUse hooks (~/.grok/docs/user-guide/
        # 10-hooks.md): JSON files in ~/.grok/hooks/*.json (global, always trusted) or
        # <project>/.grok/hooks/*.json (requires folder trust — `--trust` / /hooks-trust).
        # A hook denies a tool call via stdout {"decision":"deny","reason":...} (honored
        # regardless of exit code) or exit 2; every other failure is fail-open. The doctor
        # hook-probe still validates the mechanism end to end before guards rely on it.
        blocking_hooks=True,
        # grok has native --json-schema (implies --output-format json), but it is NOT
        # wired: native schema is a later bonus; the STEP sentinel is the contract.
        output_schema=True,
        session_capture=None,
    )

    def _build_command(self, inv: Invocation, prompt_text: str) -> tuple[list[str], str | None]:
        # re-verify against `grok --help` at doctor time; vendors drift.
        # Live-verified against grok 0.2.82 (6d0b07d2de0f):
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
        ]
        if inv.effort is not None:
            # Native effort: the headless-only `--effort` flag takes low|medium|high|xhigh
            # (+max) — a superset of cairn's EFFORTS. NOT `--reasoning-effort`: that is a
            # separate per-model reasoning knob, and both models shipped on 0.2.82 report
            # supports_reasoning_effort=false in models_cache.json (it would be a no-op).
            argv += ["--effort", inv.effort]
        return argv, None  # prompt delivered via --prompt-file; stdin is not read headlessly
