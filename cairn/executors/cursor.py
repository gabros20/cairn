"""The ``cursor`` executor — Cursor CLI, headless (``agent -p``).

Feasibility report: ``.orchestrate/xcli-feasibility-cursor.md`` (FEASIBLE-WITH-CAVEATS).

KNOWN HANG-AFTER-COMPLETION BUG (report §2, risk #1): multiple forum reports spanning Sept
2025 through at least Apr 2026 builds describe ``agent -p`` producing correct final output on
stdout but then never exiting — the process just hangs, unpredictably, across macOS/Linux/CI.
No vendor fix is confirmed as of the report. This needs NO code here: ``run_process``
(cairn/executors/base.py) already waits on the PROCESS (not the pipe) for the full
``timeout_s`` budget and SIGKILLs the whole process group on expiry — the exact mitigation
this bug calls for. Operators must size a Cursor step's ``timeout_s`` generously (the model's
own work time PLUS slack for this hang) rather than tight, since a well-behaved run and a
hung-after-success run are indistinguishable until the timeout fires.
"""

from __future__ import annotations

from pathlib import Path

from cairn.executors._cli import CliExecutor
from cairn.executors.base import Capabilities, Invocation


class CursorExecutor(CliExecutor):
    # Binary identity (report §1, [DOC/PRIOR]): the CLI's primary entrypoint is `agent`;
    # `cursor-agent` is kept only as a backward-compat alias, not the documented name going
    # forward. `name` is the literal attribute CliExecutor's shared doctor/probe/spawn
    # machinery keys off (shutil.which, `--version`, `--help`, error messages) — same
    # convention as claude/codex/grok, where `name` IS the binary. Set here (not just buried
    # inside `_build_command`'s argv literal) so it's one declared class attribute the tests
    # exercise directly (argv[0], doctor's which()/probe), per the dispatch brief.
    name = "agent"
    # cursor auto-loads BOTH AGENTS.md and CLAUDE.md at the project root (report §9) — whichever
    # of codex/claude already renders one, cursor picks it up for free. Writing neither here
    # avoids a redundant/conflicting write in a multi-executor pipeline, same reasoning as
    # grok's `_workspace_file = None` (grok.py).
    _workspace_file = None
    _install_hint = "curl https://cursor.com/install -fsS | bash"
    capabilities = Capabilities(
        # cursor ships `.cursor/hooks.json` PreToolUse-shaped hooks (report §10), but they are
        # fail-open by default and cairn's install_guards below does not wire them — leave
        # unasserted (None) rather than claim a mechanism cairn doesn't enforce.
        blocking_hooks=None,
        # cursor has native --output-format json/stream-json, but cairn does NOT wire it — the
        # STEP sentinel (in `text` output) is the sole contract today, same posture as
        # codex/grok.
        output_schema=False,
        # No --resume/--continue passed (see _build_command) → each run starts fresh (report
        # §7); nothing under a session dir for cairn to capture.
        session_capture=None,
        installs_hooks=False,  # install_guards is a no-op — cairn does not wire a cursor hook
        # Controller decision (fixed, not relitigated here): cursor ships its OWN OS-level
        # sandbox (`--sandbox enabled` + `--allow-paths`/`--network`, report §5) — directly
        # analogous to codex's `workspace-write`. cairn does NOT additionally wrap it in its
        # own OS filesystem sandbox; the argv itself carries containment, codex-style.
        sandbox="off",
    )
    # The flags `_build_command` emits — doctor re-verifies these against the installed CLI's
    # `agent --help` (W5b sub-change A.1 pattern). `--sandbox`/`--network`/`--allow-paths` are
    # report-[DOC] but the specific combination as cairn's default posture is [UNC] (report
    # §5/§12 verdict) — listed here anyway per the dispatch brief so doctor catches drift on
    # any of them, hedged or not.
    _emitted_flags: tuple[str, ...] = (
        "-p", "--workspace", "--model", "--output-format", "--sandbox", "--network",
        "--allow-paths", "-f", "--trust",
    )
    # NOTE: no `_model_findings` override (inherits CliExecutor's no-op default) — report §10:
    # no `agent models`/`agent list-models` subcommand exists, only the interactive `/model
    # [filter]` slash command, which is not scriptable for a doctor drift check. Same honest
    # gap as codex (codex.py's equivalent NOTE); cairn doesn't invent an unverified static
    # model allowlist that would only drift and give false confidence.

    def _extra_env(self, inv: Invocation) -> dict[str, str] | None:
        """W4-style config isolation (cursor's analogue of codex's ``--ignore-user-config``):
        there is no documented per-invocation flag to skip ``~/.cursor/cli-config.json``
        (report §6) — model, ``approvalMode``, sandbox defaults, etc. all live there, and a
        human operator's ambient settings would otherwise leak into every run. ``CURSOR_CONFIG_DIR``
        (report §6, [DOC (env var exists) / UNC (this specific isolation recipe — inferred, not
        vendor-documented as a determinism recipe)]) redirects where the CLI looks for that
        file, so point it at an ephemeral, cairn-owned directory UNDER the run dir — the
        machine's real config is never consulted. Created idempotently: a second invocation in
        the same run dir reuses the same (still-empty) directory rather than erroring on mkdir.

        ``CURSOR_API_KEY`` is deliberately NOT touched here — it rides ``inv.env`` passthrough
        from the workspace's env allowlist untouched, per the dispatch brief; the executor never
        reads or writes it.

        CI hygiene gap (contract rule 8, report §11): no documented env var or flag disables
        cursor's auto-update or telemetry. Inventing one would be dishonest (no [DOC]/[LIVE]
        source), so none is emitted here — tracked as a known determinism gap in the impl
        report, same as the report's own risk #3.
        """
        config_dir = inv.cwd / ".cairn" / "cursor-config"
        config_dir.mkdir(parents=True, exist_ok=True)
        return {"CURSOR_CONFIG_DIR": str(config_dir)}

    def _build_command(self, inv: Invocation, prompt_text: str) -> tuple[list[str], str | None]:
        # re-verify against `agent --help` at doctor time; vendors drift (report §1: no clean
        # semver to pin against either — `agent --version` prints a date-hash build string).
        # Verdict argv shape (report §12), UNC-hedged but emitted so doctor's flag-drift check
        # can catch removal/rename:
        argv = [
            "agent", "-p",
            # `--workspace <cwd>` overrides process-cwd inference explicitly (report §4) — cairn
            # run dirs are arbitrary, non-git directories; stated rather than left implicit.
            "--workspace", str(inv.cwd),
            "--model", inv.model,
            # `text` (default under --print): "clean, final-answer-only responses" — the mode
            # that lets a trailing `<<<STEP{...}` sentinel reach stdout undecorated (report §8).
            # NOT json/stream-json — those restructure output into an envelope cairn doesn't
            # parse.
            "--output-format", "text",
            # cursor's OWN sandbox (report §5), directly analogous to codex's
            # `--sandbox workspace-write` — path-scoped writes, network gated separately below.
            "--sandbox", "enabled",
            # W5b-style network threading (mirrors codex's `-c
            # sandbox_workspace_write.network_access=...`): emitted explicitly both ways so a
            # step's resolved policy is stated, never left to the sandbox's undeclared default.
            "--network", "true" if inv.network else "false",
            "--allow-paths", str(inv.cwd),
            # Headless has no answering surface for the default y/n command-approval prompts
            # (report §5) — `--force` (alias `--yolo`) allows commands unless explicitly denied.
            "-f",
            # `--trust` — "Trust the workspace without prompting (headless mode only)" (report
            # §5); without it an untrusted-workspace prompt has nothing to answer it either.
            "--trust",
        ]
        # No reasoning-effort flag exists on `agent -p --model` (report §3: checked against the
        # published parameter reference; absence, not omission — the only effort-like knob found
        # anywhere is a subagent-frontmatter-only feature, unrelated to this one-shot path).
        # inv.effort is therefore never threaded to argv — a documented no-op, never a crash,
        # same honesty discipline as codex's "no queryable model roster" gap above.
        # Prompt delivery: argv trailing arg (report §2 — every documented example passes the
        # prompt as a quoted positional, stdin is at most supplementary context alongside one,
        # never a replacement channel). stdin_text=None: nothing is piped.
        argv.append(prompt_text)
        return argv, None
