"""The ``claude`` executor — Anthropic's Claude Code CLI, headless."""

from __future__ import annotations

from cairn.executors._cli import CliExecutor
from cairn.executors.base import Capabilities, Invocation


class ClaudeExecutor(CliExecutor):
    name = "claude"
    _workspace_file = "CLAUDE.md"
    _install_hint = "install Claude Code and run `claude login`"
    capabilities = Capabilities(
        blocking_hooks=True,  # PreToolUse hooks can block (exit 2)
        output_schema=False,
        session_capture="~/.claude/projects/**",
    )

    def _build_command(self, inv: Invocation, prompt_text: str) -> tuple[list[str], str | None]:
        # re-verify against `claude --help` at doctor time; vendors drift.
        argv = ["claude", "-p", prompt_text, "--model", inv.model]
        if inv.effort is not None:
            argv += ["--effort", inv.effort]
        argv += ["--output-format", "text"]
        # Headless `claude -p` under the default permission mode refuses every tool use
        # ("I need your permission to write the file.") and exits 0 without producing the
        # artifact, which then fails validation. cairn's guards (blocking PreToolUse hooks —
        # Capabilities.blocking_hooks) are the enforcement layer, so run claude fully
        # non-interactive and let the guards, not an interactive prompt, gate tools.
        argv += ["--permission-mode", "bypassPermissions"]
        return argv, None  # prompt is an argv arg, nothing on stdin
