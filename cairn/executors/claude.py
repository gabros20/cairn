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
        return argv, None  # prompt is an argv arg, nothing on stdin
