"""The ``opencode`` executor — anomalyco/opencode CLI, headless (``opencode run``).

Feasibility: .orchestrate/xcli-feasibility-opencode.md. Two load-bearing facts from that report
drive this file's shape, both [DOC]+[LIVE]:

  * §5 — ``--auto`` is opencode's own policy-layer "auto-approve" switch, NOT an OS sandbox (no
    seatbelt/landlock/namespace mechanism was found). cairn therefore wraps every invocation in
    its own OS filesystem sandbox (``capabilities.sandbox = "fs"``, the C8/W3c ``_sandbox()``
    machinery already shared with claude) instead of trusting opencode's permission model as the
    security boundary.
  * §6 — there is no ``--ignore-user-config`` flag. Isolation is achieved by relocating opencode's
    XDG dirs (it is fully XDG-scoped — live-verified via ``opencode debug paths``) under the run
    dir via ``_extra_env``, which also isolates ``auth.json``/plugins/session history that a
    single ignore-flag would have missed.
"""

from __future__ import annotations

from cairn.executors._cli import CliExecutor
from cairn.executors.base import Capabilities, Invocation


class OpencodeExecutor(CliExecutor):
    name = "opencode"
    _workspace_file = "AGENTS.md"  # report §9: AGENTS.md beats CLAUDE.md at the same tier
    _install_hint = (
        "install opencode (curl -fsSL https://opencode.ai/install | bash) and set a provider "
        "API key env var (e.g. ANTHROPIC_API_KEY) — or run `opencode auth login` interactively "
        "once outside cairn; cairn never writes to opencode's auth.json"
    )
    capabilities = Capabilities(
        # Contract rule 1 (shared brief): honest-by-default for a CLI cairn hasn't hook-probed or
        # native-schema-wired yet — every field below states what cairn itself does, not what the
        # vendor CLI is theoretically capable of.
        blocking_hooks=None,   # opencode DOES support blocking hooks via a JS/TS plugin (report
        # §10), but that requires shipping a plugin file, not a CLI flag — cairn's install_guards
        # below does not wire it (installs_hooks=False), so this stays an open question for the
        # doctor hook-probe, same posture as grok's documented-but-unwired hooks.
        output_schema=False,   # STEP sentinel is the sole contract; --format json is deliberately
        # NOT used (report §8: it wraps text inside per-event objects, so a bare sentinel would be
        # JSON-escaped rather than sitting in raw stdout) — --format default is what _build_command
        # emits below.
        session_capture=None,  # opencode persists session state to disk (report §7) under
        # XDG_DATA_HOME, which _extra_env relocates into the run dir's throwaway scratch — nothing
        # for cairn to glob-capture into logs/ that isn't already there via the run dir itself.
        installs_hooks=False,  # install_guards is the base no-op — cairn does not ship the plugin
        # file that would be required to wire opencode's blocking-hook mechanism.
        # C8/W3c: opencode's own `--auto` is a policy trust switch, not a jail (see module
        # docstring §5) — wrap every invocation in cairn's OS filesystem sandbox.
        sandbox="fs",
    )
    # The flags `_build_command` emits — doctor re-verifies these against the installed CLI's
    # `opencode run --help` (W5b sub-change A.1 pattern, mirrored from codex/claude/grok).
    _emitted_flags: tuple[str, ...] = ("--dir", "--model", "--format", "--auto")
    # NOTE: no `_model_findings` override (inherits CliExecutor's no-op default). opencode DOES
    # expose a queryable roster (`opencode models [provider] [--refresh]`, report §3/§10), but
    # unlike grok's `grok models` the feasibility report has no raw captured output for this
    # machine's gateway catalog to parse against (report §3 only paraphrases example slugs, it
    # does not include a `.orchestrate/raw/*.txt` capture the way grok's does) — inventing a
    # parser for an unverified output shape would be worse than no check at all (same reasoning
    # codex's file gives for omitting this hook; codex-F-style honesty discipline).

    def _help_argv(self) -> list[str]:
        # `--dir`/`--model`/`--format`/`--auto` are flags of the `run` subcommand, not the
        # top-level `opencode --help` (report §2-§5 all cite `opencode run --help` specifically).
        return ["opencode", "run", "--help"]

    def _extra_env(self, inv: Invocation) -> dict[str, str] | None:
        """Relocate opencode's four XDG dirs under the run dir (report §6) and disable the
        Claude Code global-rules fallback tier (report §9) — the ONLY config-isolation channel
        available, since there is no `--ignore-user-config` flag (module docstring).

        Per-INVOCATION home lives at ``<run_dir>/.cairn/opencode-xdg.<log_path.stem>/<kind>``
        (contract rule 3: generated homes live under the run dir), created idempotently here — a
        second invocation with the SAME ``log_path`` stem (a retried attempt) reuses the same
        (already-populated) scratch rather than erroring.

        Keyed on ``inv.log_path.stem`` (unique per step+attempt+cycle, walk.py's ``_log_path``),
        NOT on ``inv.cwd`` alone: ``inv.cwd`` is the run dir SHARED by every step in a run (walk.py
        sets ``cwd=self.run_dir`` unconditionally), and a ``ParallelNode`` runs its children
        concurrently on the SAME executor instance. Deriving the base from ``inv.cwd`` alone let
        two parallel opencode steps share ONE ``XDG_DATA_HOME``/``auth.json``/session store — the
        exact cross-session isolation this relocation exists to provide would be void, and two live
        opencode processes writing to one session DB is an opencode-internal corruption risk
        (xcli-review-quality.md H1). Keying per-invocation also means a retried attempt gets a
        FRESH scratch rather than reusing another attempt's stale session state.

        CAUTION (dispatch message): cairn's walker already sets ``XDG_STATE_HOME`` ambient in
        ``inv.env`` for gatekeys on the claude executor (its PreToolUse hook subprocess reads the
        signed guard manifest from a state-dir path). This override WINS by design — `_extra_env`
        is merged over `inv.env` at spawn (`_cli.py::invoke`) — and that is safe here specifically
        because `installs_hooks=False` above: opencode's `install_guards` is the base no-op
        (`_cli.py::CliExecutor.install_guards`), so no cairn hook-check subprocess is ever spawned
        under this executor that could need the walker's gatekeys-scoped `XDG_STATE_HOME`. Nothing
        reads that value here; shadowing it with opencode's own state dir is inert from cairn's
        guard-enforcement point of view.
        """
        base = inv.cwd / ".cairn" / f"opencode-xdg.{inv.log_path.stem}"
        xdg = {
            "XDG_CONFIG_HOME": base / "config",
            "XDG_DATA_HOME": base / "data",
            "XDG_CACHE_HOME": base / "cache",
            "XDG_STATE_HOME": base / "state",
        }
        for path in xdg.values():
            path.mkdir(parents=True, exist_ok=True)
        env = {k: str(v) for k, v in xdg.items()}
        # report §9: the Claude Code doctrine-fallback tier is keyed to `$HOME/.claude`, not XDG,
        # so relocating XDG_CONFIG_HOME alone does not suppress it — it needs its own disable.
        env["OPENCODE_DISABLE_CLAUDE_CODE"] = "1"
        # report §11 (CI/env hygiene): no background update checks, and no terminal-title escape
        # sequences landing in the combined stdout+stderr stream cairn captures/parses.
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        env["OPENCODE_DISABLE_TERMINAL_TITLE"] = "1"
        return env

    def _build_command(self, inv: Invocation, prompt_text: str) -> tuple[list[str], str | None]:
        # re-verify against `opencode run --help` at doctor time; vendors drift (near-daily
        # patch releases per report §1).
        argv = [
            "opencode", "run",
            # report §4: cwd-equivalent flag. Set (not relied on Popen's own cwd= alone) since
            # `--dir` vs process-cwd semantics for tool execution aren't fully documented [UNC];
            # run_process (base.py) still also spawns with cwd=inv.cwd, so both agree.
            "--dir", str(inv.cwd),
            # report §3: format is `provider/model`; resolve_model passes cairn's configured tier
            # string through untouched (CliExecutor.resolve_model, no opencode-specific override
            # needed — the tier spec IS the full provider/model string).
            "--model", inv.model,
            # report §8: the ONLY format whose stdout is raw prose the sentinel regex can find —
            # `--format json` wraps text inside per-event JSON objects instead.
            "--format", "default",
            # report §5: "auto-approve permissions that are not explicitly denied (dangerous!)" —
            # a policy switch, not a jail; safe here ONLY because capabilities.sandbox="fs" wraps
            # this argv in cairn's own OS filesystem sandbox before spawn (see _cli.py::invoke).
            "--auto",
        ]
        # Dispatch decision (controller, not re-litigated here): no effort flag is emitted. The
        # feasibility report (§3) does document a live `--variant <value>` flag described as
        # "provider-specific reasoning effort, e.g., high, max, minimal" — but that vocabulary
        # does not line up with cairn's EFFORTS (low|medium|high|xhigh|max: no documented
        # "low"/"xhigh", and "minimal" has no cairn equivalent), and support is per-provider/
        # per-model [UNC], so wiring it would mean inventing an unverified mapping. inv.effort is
        # therefore accepted and intentionally DROPPED here, never a crash — see
        # Capabilities/doctor: nothing asserts opencode honors cairn's effort tiers.
        return argv, prompt_text  # prompt delivered on stdin (report §2: resolveRunInput's
        # stdin-fallback path); no positional `message` argv, matching the codex.py pattern and
        # avoiding the "message + stdin combined" behavior report §2 flags when both are given.
