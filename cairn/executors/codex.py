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
        # codex has native --output-schema (see _build_command note below), but cairn does NOT
        # wire it — the STEP sentinel is the sole contract today (docs/API.md §7); native schema
        # is a later bonus. W5b sub-change B: this previously asserted True, overstating what
        # cairn actually uses (codex-F15).
        output_schema=False,
        # W5b sub-change B (codex-F15 mirror of claude-F7, W4): codex now runs with --ephemeral
        # (below), so there are no session files under ~/.codex/sessions/** to capture — the old
        # glob was dead, no code ever consumed it.
        session_capture=None,
        installs_hooks=False,  # install_guards is a no-op — cairn does not wire a codex hook yet
    )
    # The flags `_build_command` emits — doctor re-verifies these against the installed CLI's
    # `codex exec --help` (W5b sub-change A.1); `-c` covers both the effort and network
    # overrides below (a single dotted-path-override flag, used twice).
    _emitted_flags: tuple[str, ...] = (
        "-C", "-m", "--sandbox", "--skip-git-repo-check", "--ephemeral",
        "--ignore-user-config", "--ignore-rules", "-c",
    )
    # NOTE (W5b sub-change A.2): codex has no queryable model roster — `codex --help` and
    # `codex exec --help` list no `models`/`list-models` subcommand (verified against the
    # captured help in .orchestrate/raw/), unlike grok's `grok models`. A bad `-m` just errors
    # loudly at run time instead. No `_model_findings` override here (inherits CliExecutor's
    # no-op default): cairn doesn't invent an unverified static model allowlist that would only
    # drift and give false confidence.

    def _help_argv(self) -> list[str]:
        return ["codex", "exec", "--help"]

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
            # W5b sub-change B (codex-F15 mirror of claude-F7): run session-less so
            # Capabilities.session_capture=None above is genuinely true, not just unconsumed.
            # Confirmed in the captured `codex exec --help`: "Run without persisting session
            # files to disk".
            "--ephemeral",
            # W4 config isolation (codex-F6): seal the process from ambient user config so
            # identical pipeline runs are deterministic. `--ignore-user-config` skips
            # `$CODEX_HOME/config.toml` (auth still uses CODEX_HOME); `--ignore-rules` skips user
            # or project execpolicy `.rules` files. Both confirmed in the captured
            # `codex exec --help`.
            "--ignore-user-config",
            "--ignore-rules",
            # W5b sub-change C (codex-F5): thread the step's resolved network policy through —
            # StepNode.network was parsed since plan.py but never reached codex until
            # Invocation grew this field. `-c` takes a dotted-path override whose value is
            # parsed as TOML (`true`/`false` are literal TOML booleans — captured help: "the
            # `value` portion is parsed as TOML"). `sandbox_workspace_write.network_access` is
            # documented in OpenAI's published config reference (developers.openai.com/codex/
            # config-reference, linked from the codex repo's docs/config.md) as: "Allow outbound
            # network access inside the workspace-write sandbox." Emitted unconditionally (not
            # only under `network: true`) so a step's `false` is stated explicitly rather than
            # left to the sandbox's undeclared default.
            "-c", f"sandbox_workspace_write.network_access={'true' if inv.network else 'false'}",
        ]
        if inv.effort is not None:
            argv += ["-c", f"model_reasoning_effort={inv.effort}"]
        return argv, prompt_text  # prompt delivered on stdin
