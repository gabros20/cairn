"""The ``grok`` executor — xAI Grok CLI, headless. BYOK model aliases bake the reasoning
effort, so Grok has no effort flag and ``resolve_model`` always returns effort None."""

from __future__ import annotations

from cairn.executors._cli import CliExecutor
from cairn.executors.base import Capabilities, Invocation


class GrokExecutor(CliExecutor):
    name = "grok"
    _workspace_file = None  # grok reads CLAUDE.md AND AGENTS.md; it writes neither
    _install_hint = "install the grok CLI and run its setup (BYOK effort aliases)"
    capabilities = Capabilities(
        blocking_hooks=True,  # exit-2 blocking confirmed in research
        output_schema=False,
        session_capture=None,
    )

    def resolve_model(self, tier: str, effort: str) -> tuple[str, str | None]:
        # Effort is baked into the model alias — always drop it, whatever the tier says.
        model, _ = super().resolve_model(tier, effort)
        return (model, None)

    def _build_command(self, inv: Invocation, prompt_text: str) -> tuple[list[str], str | None]:
        # re-verify against `grok --help` at doctor time; vendors drift.
        argv = [
            "grok", "-p",
            "--cwd", str(inv.cwd),
            "-m", inv.model,
            "--output-format", "text",
            "--permission-mode", "dontAsk",
            "--no-alt-screen",
            "--no-auto-update",
        ]
        return argv, prompt_text  # prompt delivered on stdin; no effort flag exists
