"""The ``codex`` executor — OpenAI Codex CLI, headless (``codex exec``)."""

from __future__ import annotations

from cairn.executors._cli import CliExecutor
from cairn.executors.base import Capabilities, Invocation


class CodexExecutor(CliExecutor):
    name = "codex"
    _workspace_file = "AGENTS.md"
    _install_hint = "npm i -g @openai/codex"
    capabilities = Capabilities(
        # Headless blocking-hook support is UNVERIFIED — leave None so the doctor probe decides.
        blocking_hooks=None,
        output_schema=True,  # codex has native --output-schema (see _build_command note)
        session_capture="~/.codex/sessions/**",
    )

    def _build_command(self, inv: Invocation, prompt_text: str) -> tuple[list[str], str | None]:
        # re-verify against `codex exec --help` at doctor time; vendors drift.
        # Live-verified against codex-cli 0.142.5:
        #   * `-a/--ask-for-approval` no longer exists on `codex exec` (argv error); exec mode
        #     is hardwired non-interactive with `approval: never`, so nothing replaces it.
        #   * `--skip-git-repo-check` is required: without it codex refuses any cwd that is
        #     neither a git repo nor a trusted directory, and cairn run dirs are arbitrary.
        #     cairn's sandbox flag + guards are the enforcement layer, not codex's trust gate.
        argv = [
            "codex", "exec",
            "-C", str(inv.cwd),
            "-m", inv.model,
            "--sandbox", "workspace-write",
            "--skip-git-repo-check",
        ]
        if inv.effort is not None:
            argv += ["-c", f"model_reasoning_effort={inv.effort}"]
        # NOTE: output_schema is True, but we do NOT wire --output-schema yet — the STEP
        # sentinel is the contract (docs/API.md §7); native schema is a later bonus.
        return argv, prompt_text  # prompt delivered on stdin
