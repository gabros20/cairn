"""The ``agy`` executor — Google's Antigravity CLI, headless (``agy -p``).

**LOCAL-ONLY executor.** agy has NO CI-compatible auth path: its only working credential
channels are (a) a pre-existing interactive OS-keyring sign-in (Keychain / Linux Secret Service /
Windows Credential Manager) or (b) a manual SSH browser-URL-and-code paste. A Google Antigravity
collaborator confirmed directly on the tracking issue that API-key / service-account auth is NOT
supported today and pointed CI users at a different product (the Antigravity SDK).
[DOC — https://github.com/google-antigravity/antigravity-cli/issues/78] The controller arbitration
(feasibility §CONTROLLER ARBITRATION) narrows agy to LOCAL runs on exactly the same footing as
cairn's own ``claude``/``codex`` executors, which likewise find their stored OAuth credential via
the user's OS keychain — the credential never rides env, it is UID/session-scoped, and the user
logs in once interactively. ``doctor`` therefore ERRORs actionably when agy looks unauthenticated
(see ``_model_findings``).

**stdout reliability is smoke-gated.** Headless stdout drop was agy's single most-reported defect
for its first ~2 months (GH #76: full model round-trips completing with zero bytes reaching a
piped/redirected stdout — exactly cairn's ``Popen``-with-captured-stdout pattern). It was fixed
PIECEMEAL across five releases ending 1.1.4 (2026-07-18). Because the last fix is days old and no
independent post-1.1.4 confirmation exists, the STEP-sentinel-reaches-stdout contract is treated as
UNVERIFIED until a live smoke test runs against a pinned ≥1.1.4 binary (report §2/§8).

Three smoke items gate the first real run (arbitration):
  1. Does keyring auth survive a relocated ``HOME``? (Keyring creds are reportedly NOT HOME-scoped
     — [community §6] — so a per-run generated HOME should isolate settings/config without
     breaking sign-in. Overriding HOME in ``_extra_env`` means the child's keychain/keyring access
     relies on UID/session, NOT HOME — [community]-sourced, item #1.)
  2. Does the interactive workspace-trust gate (report §4 — no ``--skip-trust`` flag exists) block
     a ``-p`` run against a fresh ``--cwd`` from a fresh HOME, and is the trust store file-seedable
     under the relocated home? [UNC — do NOT invent a trust-file format; we seed only the settings
     paths we KNOW.]
  3. stdout reliability on a pinned ≥1.1.4 binary (GH #76/§8).

Design shape (from the arbitration — do not relitigate):
- Per-invocation generated ``HOME`` (kimi's relocated-home pattern) is agy's ONLY config-isolation
  channel: there is no ``--ignore-user-config`` flag (report §6), and ``last_conversations.json``
  keys ``cwd → conversation_id`` so repeated ``-p`` calls against one cwd could implicitly resume
  (report §7). A throwaway HOME defuses both and lets cairn pre-seed ``settings.json``.
- ``--sandbox`` is DROPPED from argv; posture is ``fs`` (cairn's own OS FS-wrap). Nesting cairn's
  sandbox-exec around agy's own sandbox-exec is a realistic breakage, and the researcher already
  recommended trusting cairn's wrap over agy's native one (report §5). ``--dangerously-skip-
  permissions`` IS emitted — the FS wrap + the ``post`` validator are the hard gates (same
  bypassPermissions-reduction posture as claude/kimi).
- Effort is accepted-and-dropped (opencode precedent): agy's only effort lever is an [UNC]
  suffixed model slug (e.g. ``"Gemini 3.5 Flash (Low)"`` — report §3, community-inferred, not in
  official docs). Operators who want an effort variant encode it in the tier's model slug; cairn
  does not synthesize one. ``inv.effort`` flows in via ``resolve_model`` and is intentionally not
  emitted.

Every fact is tagged [DOC]/[community]/[UNC] from .orchestrate/xcli-feasibility-agy.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from cairn.executors._cli import CliExecutor, probe_argv
from cairn.executors.base import Capabilities, Finding, Invocation
from cairn.kernel.errors import CairnError

# Default provider-side settings written into the per-run `settings.json`. These are the settings
# keys whose PATHS we know from the docs/CHANGELOG (report §5/§11) — we deliberately write ONLY
# these and do NOT invent a trust-file format for the workspace-trust gate (smoke item #2).
_DEFAULT_SETTINGS = {
    # report §11: telemetry defaults ON with no per-invocation flag/env override — the settings.json
    # boolean is the only lever, so seal it off by policy.
    "enableTelemetry": False,
    # report §5: `enableTerminalSandbox` defaults to false; set it explicitly so a stray global
    # config under a different HOME can never flip agy into its OWN OS sandbox underneath cairn's
    # FS wrap (the nested-seatbelt breakage the arbitration dropped `--sandbox` to avoid).
    "enableTerminalSandbox": False,
}

# `agy models` output format is [UNC] — no live capture exists (agy is not installed in the
# research env). Parse DEFENSIVELY: one bullet-marked line per model, capturing the WHOLE rest of
# the line (agy slugs contain spaces, e.g. "Gemini 3.5 Flash", unlike grok's single-token slugs),
# then strip a trailing "(default)" annotation. Anything unparseable degrades to a single warning,
# never a crash (see `_model_findings`). Re-verify this regex against a real `agy models` capture
# the first time agy is installed.
_AGY_MODEL_LINE_RE = re.compile(r"^\s*[*-]\s+(.+?)(?:\s+\(default\))?\s*$", re.MULTILINE)


def _parse_agy_models(text: str) -> set[str]:
    """Best-effort set of model slugs from ``agy models`` output. Empty set ⇒ unparseable (the
    caller emits one warning and skips the drift check). Format is [UNC] — see ``_AGY_MODEL_LINE_RE``."""
    return {m.group(1).strip() for m in _AGY_MODEL_LINE_RE.finditer(text) if m.group(1).strip()}


def render_settings_json() -> str:
    """The per-run ``<home>/.gemini/antigravity-cli/settings.json`` (report §6 resolves settings
    from ``$HOME/.gemini/antigravity-cli/settings.json``; the relocated HOME re-roots it here).

    Contains only settings whose paths are KNOWN from the docs (telemetry off, terminal sandbox
    off explicitly). NO secret is ever written (brief rule 4 — agy has no key-in-config path
    anyway; auth is OS-keyring). The workspace-trust store is NOT seeded — its file format is
    [UNC] and inventing one is forbidden (smoke item #2)."""
    return json.dumps(_DEFAULT_SETTINGS, indent=2, sort_keys=True) + "\n"


class AgyExecutor(CliExecutor):
    name = "agy"
    # agy auto-parses AGENTS.md (or GEMINI.md) at the workspace root (report §9). AGENTS.md is the
    # same file codex/kimi write — rendering is idempotent (base.render_workspace only writes on
    # missing/changed content), so co-resident executors do not collide.
    _workspace_file = "AGENTS.md"
    # report §1: the official installer one-liner. There is no npm/brew package to name.
    _install_hint = "curl -fsSL https://antigravity.google/cli/install.sh | bash"
    capabilities = Capabilities(
        # Headless blocking-hook firing during a `-p` run is UNVERIFIED (report §10) and cairn
        # wires no agy hook — leave None so the doctor probe decides, never assert True.
        blocking_hooks=None,
        # No `--output-format`/`--json`/`stream-json` has ever shipped (report §8, full CHANGELOG
        # audited) — plain text only; the STEP sentinel is the sole contract.
        output_schema=False,
        # Structural, not flag-driven: the per-run HOME is a throwaway dir under the run dir, so
        # whatever conversation/session record agy persists under it (brain/, SQLite/JSONL — §7)
        # is discarded with it. cairn never reads it back; no `--ephemeral` flag exists.
        session_capture=None,
        installs_hooks=False,  # install_guards is the base no-op — cairn wires no agy hook yet
        # `agy -p --dangerously-skip-permissions` auto-approves every tool with NO native OS
        # sandbox (we deliberately DROP agy's `--sandbox` to avoid nesting it under cairn's own —
        # arbitration). Same threat class as claude/kimi bypassPermissions → cairn's OS FS wrap +
        # the `post` validator are the hard gates.
        sandbox="fs",
    )
    # Exactly what `_build_command` emits — doctor re-verifies each against the installed
    # `agy --help` (drift check). Effort is NOT here (accepted-and-dropped; no flag). `--sandbox`
    # is NOT here (deliberately dropped from argv — arbitration).
    _emitted_flags: tuple[str, ...] = (
        "-p", "--cwd", "--model", "--dangerously-skip-permissions", "--print-timeout",
    )

    def _model_findings(self) -> list[Finding]:
        """agy HAS a queryable roster (`agy models`, report §10). Two jobs here:

        1. AUTH gate. `agy models` reads the roster for the logged-in account, so a failure/timeout
           is the strongest local signal that agy is unauthenticated — and agy auth is
           interactive-only with NO CI path (report §1). Emit an actionable ERROR (arbitration:
           doctor must ERROR actionably when unauthenticated), not a generic "could not run".
        2. Drift check (grok-style, best-effort). If the roster parses, warn on any configured tier
           model that isn't in it. The output FORMAT is [UNC] (agy uninstalled — no live capture),
           so parse defensively: unparseable ⇒ one warning, never a crash.
        """
        try:
            code, out = probe_argv([self.name, "models"])
        except (OSError, CairnError):
            code, out = None, ""
        if code is None or code != 0 or not out:
            # Most likely cause on a machine that HAS the binary: no interactive sign-in yet.
            return [
                Finding(
                    "error",
                    f"`{self.name} models` failed or timed out — agy auth is interactive-only: "
                    f"run `{self.name}` once locally to sign in via the OS keyring/browser. CI has "
                    f"NO auth path (Google-confirmed, GH #78).",
                    fix=f"run `{self.name}` locally once to sign in (no env/API-key auth path exists)",
                )
            ]
        known = _parse_agy_models(out)
        if not known:
            # Format drifted from what we can parse (it is [UNC] to begin with) — say so and skip,
            # rather than crash or emit a false "unknown model" for every tier.
            return [
                Finding(
                    "warning",
                    f"`{self.name} models` returned no parseable model list — skipping model drift "
                    f"check (output format is unverified pending a live capture)",
                )
            ]
        findings: list[Finding] = []
        for tier in sorted(self.config.tiers):
            model = self.config.tiers[tier].model
            if model not in known:
                findings.append(
                    Finding(
                        "warning",
                        f"{self.name} tier {tier!r} model {model!r} not in `{self.name} models` "
                        f"(known: {', '.join(sorted(known))})",
                    )
                )
        return findings

    def _extra_env(self, inv: Invocation) -> dict[str, str]:
        """Generate the throwaway per-INVOCATION ``HOME`` and pre-seed agy's ``settings.json`` under
        it (report §6 — agy's only config-isolation channel; there is no ``--ignore-user-config``).

        Keyed on ``inv.log_path.stem`` (unique per step+attempt+cycle, walk.py's ``_log_path``), NOT
        on ``inv.cwd`` alone: ``inv.cwd`` is the run dir SHARED by every step of a run, and a
        ``ParallelNode`` runs its children concurrently on the SAME executor instance. Deriving the
        home from ``inv.cwd`` alone would let two parallel agy steps race to write one
        ``settings.json`` and share one ``last_conversations.json`` (cwd→conversation reuse, §7).
        Per-invocation keying gives each attempt a fresh, isolated home. (This mirrors the kimi
        KIMI_CODE_HOME fix, xcli-review-quality.md H1.)

        CAUTION (smoke item #1): overriding ``HOME`` here means the child's keychain/keyring access
        relies on UID/session, NOT on HOME — [community]-sourced, and the reason keyring auth is
        expected to survive the relocation (creds are not HOME-scoped, report §6). We NEVER touch
        ``USER``/``LOGNAME`` (walk.py's sealed baseline passes those through, same as claude/codex).
        """
        home = Path(inv.cwd) / ".cairn" / f"agy-home.{inv.log_path.stem}"
        # report §6: settings resolve from `$HOME/.gemini/antigravity-cli/settings.json`.
        settings_dir = home / ".gemini" / "antigravity-cli"
        settings_dir.mkdir(parents=True, exist_ok=True)
        (settings_dir / "settings.json").write_text(render_settings_json(), encoding="utf-8")
        return {
            "HOME": str(home),
            # report §11: the background self-updater otherwise holds an advisory lock file that can
            # block concurrent invocations — disable it for headless determinism.
            "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
        }

    def _build_command(self, inv: Invocation, prompt_text: str) -> tuple[list[str], str | None]:
        # re-verify against `agy --help` at doctor time; vendors drift.
        argv = [
            "agy",
            # [DOC §2] Prompt as `-p`'s INLINE argument, never via stdin. Since CHANGELOG 1.1.1 agy
            # only reads stdin when `-p` is given with NO inline arg — this was the deliberate fix
            # for `agy -p` hanging forever inside scripts/subprocesses that lack an interactive
            # stdin. So stdin_text is None (see the return) and the envelope rides argv.
            "-p", prompt_text,
            # [DOC §4] Headless working dir. NOTE: the interactive workspace-trust gate (§4, no
            # `--skip-trust` flag exists) may block a `-p` run against a fresh cwd — smoke item #2.
            "--cwd", str(inv.cwd),
            # [DOC §3] Pin the model. Print mode hard-fails (non-zero + lists models) if unresolved
            # since 1.1.2, rather than silently downgrading — good for a pipeline. Effort variants,
            # if wanted, are encoded IN this slug by the operator (report §3 [UNC] suffixed-slug);
            # cairn does not synthesize one (inv.effort accepted-and-dropped, see module docstring).
            "--model", inv.model,
            # [DOC §5] Non-interactive tool approval. `--sandbox` is DELIBERATELY NOT emitted
            # (arbitration): nesting agy's own OS sandbox under cairn's FS wrap is a realistic
            # breakage, so cairn's `fs` posture + the `post` validator are the gates instead.
            "--dangerously-skip-permissions",
            # [DOC §2] Bound the print-mode run. agy takes a duration string (e.g. `30s`). Emitted
            # with a 5s MARGIN under cairn's own budget (quality-review A2-L1): run_process starts
            # its clock first, so an equal bound means cairn's group-SIGKILL almost always wins the
            # tie and agy's graceful fail-fast path (the 1.1.2 fix, actionable stderr + non-zero
            # exit) never gets to run. The margin lets agy fail first; run_process stays the hard
            # ceiling. max(1, …) keeps a tiny test budget valid for the duration grammar.
            "--print-timeout", f"{max(1, int(inv.timeout_s) - 5)}s",
        ]
        # NOTE (network): inv.network is NOT consumed here. agy exposes no standalone network on/off
        # flag (report §10) — only the permission engine's per-domain ask-by-default actions, which
        # cairn does not drive. The OS FS sandbox is the only containment layer (fs posture keeps
        # network ON). Not a silent drop — there is simply no agy lever to wire, same shape as
        # grok/kimi.
        return argv, None  # prompt on argv (§2); stdin is never read
