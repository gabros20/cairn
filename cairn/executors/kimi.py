"""The ``kimi`` executor — MoonshotAI's Kimi Code CLI, headless (``kimi -p``).

Distinct from the deprecated legacy Python ``kimi-cli`` — this is the Node CLI
``@moonshot-ai/kimi-code`` (binary ``kimi``, v0.28.0 at the time of writing). Every fact
cited below is tagged [DOC]/[LIVE]/[UNC] from the feasibility report
(.orchestrate/xcli-feasibility-kimi.md), itself sourced from a live-probed-against-v0.28.0
research set.

Two things make kimi unlike claude/codex/grok and shape this file:

1. **No per-invocation effort flag and no config-skip flag.** kimi has neither a
   ``--effort``-style flag (effort lives only in ``config.toml`` under a model alias's
   ``overrides.reasoning_effort``) nor a ``--ignore-user-config`` equivalent. The ONE lever
   for both determinism and effort is a relocated data root: ``KIMI_CODE_HOME`` pointed at a
   throwaway per-run dir holding a cairn-written ``config.toml`` that defines a single model
   alias ``cairn`` carrying the provider + model + mapped effort. ``_build_command`` always
   emits ``-m cairn``; ``_extra_env`` writes that config and points ``KIMI_CODE_HOME`` at it.
   [DECISION §3/§6]

2. **BYO-provider-API-key auth only, key NEVER on disk.** Relocating ``KIMI_CODE_HOME`` also
   isolates ``credentials/`` (OAuth tokens live under the same root), so managed-service OAuth
   is out of scope for cairn (report §6). The generated ``config.toml`` names the provider and,
   in a ``[providers.cairn.env]`` sub-table, the ENV VAR NAME kimi reads the key from — never
   the key value (brief rule 4). cairn already passes that var through ``inv.env``.

Sandbox posture is ``fs`` (OS FS-wrap), same as claude: ``kimi -p`` forces the ``auto``
permission policy with no native OS sandbox — the same threat class as claude's
``bypassPermissions`` (report §5).
"""

from __future__ import annotations

from pathlib import Path

from cairn.executors._cli import CliExecutor
from cairn.executors.base import Capabilities, Invocation

# cairn EFFORTS (low|medium|high|xhigh|max) → kimi's three-value reasoning_effort vocabulary
# (low|high|max — report §3). kimi has NO middle value, so `medium` maps UP to kimi's own vendor
# default (`high`); `low`→`low`, `high`→`high`, `xhigh`/`max`→`max`. Documented, pure, tested.
_EFFORT_MAP = {
    "low": "low",
    "medium": "high",  # kimi has no middle rung; its vendor default IS high, so medium maps up
    "high": "high",
    "xhigh": "max",
    "max": "max",
}
# Unmappable → nearest sensible value, never a crash (brief rule 5). `high` is kimi's own default,
# so an unexpected effort string degrades to the vendor default rather than erroring.
_EFFORT_FALLBACK = "high"

# Config-side defaults for the BYO provider, overridable per workspace via
# `[executors.kimi.flags]` (ExecutorConfig.flags is an open table). `provider_type` is the kimi
# provider `type` written into `[providers.cairn]` (one of kimi|anthropic|openai|
# openai_responses|google-genai|vertexai — report §1); `api_key_env` is the NAME of the env var
# kimi reads the key from (its VALUE never touches disk — brief rule 4).
_DEFAULT_PROVIDER_TYPE = "kimi"
_DEFAULT_API_KEY_ENV = "KIMI_API_KEY"


def map_effort(effort: str) -> str:
    """cairn effort → kimi reasoning_effort (see ``_EFFORT_MAP``). Pure; unmappable → the vendor
    default (``high``), never a crash."""
    return _EFFORT_MAP.get(effort, _EFFORT_FALLBACK)


def _toml_str(value: str) -> str:
    """Quote ``value`` as a TOML basic string. A hand-rolled emitter is used instead of a TOML
    library because ``tomllib`` is read-only and no new dep is allowed (brief); the values here are
    controlled identifiers (provider type, model slug, effort, an env-var name), but backslash and
    double-quote are still escaped so a stray one can never break — or worse, widen — the config.
    A raw newline/carriage-return is ALSO escaped (not rejected): a TOML basic string is single-line
    by grammar, so an unescaped ``\\n`` would either error kimi's parser or, worse, let a value
    smuggle in a new line that reads as its own TOML key/table row — escaping it to the two-char
    ``\\n``/``\\r`` sequence is the simpler, non-fatal fix (operator-controlled values only; see the
    module docstring — this is robustness against a foot-gun, not an agent-reachable trust boundary)."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


def render_config_toml(
    *, provider_type: str, api_key_env: str, model: str, effort: str | None
) -> str:
    """The per-run ``config.toml`` written under ``KIMI_CODE_HOME`` (report §6).

    Defines exactly one model alias ``cairn`` — the alias ``_build_command`` always selects with
    ``-m cairn`` — carrying the provider, the resolved model, and (when set) the kimi-mapped
    reasoning effort. ``[providers.cairn.env] api_key`` names the ENV VAR kimi reads the key from,
    never the key itself (brief rule 4). ``effort`` here is ALREADY kimi-mapped (``map_effort``);
    when None the whole ``overrides`` table is omitted so kimi falls back to the model's own default.
    """
    lines = [
        'default_model = "cairn"',
        "",
        "[providers.cairn]",
        f"type = {_toml_str(provider_type)}",
        "",
        # kimi reads the key from this NAMED env var (report §1: providers accept an `env`
        # sub-table pointing at a shell env var) — the value stays in inv.env, off disk.
        "[providers.cairn.env]",
        f"api_key = {_toml_str(api_key_env)}",
        "",
        '[models."cairn"]',
        'provider = "cairn"',
        f"model = {_toml_str(model)}",
    ]
    if effort is not None:
        lines += [
            "",
            '[models."cairn".overrides]',
            f"reasoning_effort = {_toml_str(effort)}",
        ]
    return "\n".join(lines) + "\n"


class KimiExecutor(CliExecutor):
    name = "kimi"
    _workspace_file = "AGENTS.md"  # same context file codex uses (report §9) — no new collision
    _install_hint = "npm i -g @moonshot-ai/kimi-code"
    capabilities = Capabilities(
        # Headless blocking-hook firing is UNVERIFIED and kimi hooks fail OPEN (report §5/§10) —
        # leave None so the doctor probe decides, never assert True for a mechanism cairn neither
        # installs nor has live-verified. The OS FS sandbox + the `post` validator are the hard
        # gate, same posture as claude.
        blocking_hooks=None,
        # kimi has `--output-format stream-json`, but cairn does NOT wire it — the STEP sentinel is
        # the sole contract (docs/API.md §7). `text` mode's per-line `•` prefix is sentinel-safe
        # (report §2: parse_step_sentinel scans for `<<<STEP` anywhere, not line-anchored).
        output_schema=False,
        # Structural, not flag-driven: KIMI_CODE_HOME is a throwaway per-run dir under the run dir,
        # so whatever session record kimi writes is discarded with it — no `--ephemeral` needed
        # (report §7). cairn never reads it back.
        session_capture=None,
        installs_hooks=False,  # install_guards is the base no-op — cairn wires no kimi hook yet
        # `kimi -p` auto-approves every tool with NO native OS sandbox — the same threat class as
        # claude's bypassPermissions (report §5), so cairn wraps the process in its OS FS sandbox.
        sandbox="fs",
    )
    # The flags `_build_command` emits — doctor re-verifies each is still advertised by the
    # installed `kimi --help` (W5b drift check). Effort is NOT a flag (it rides config.toml), so it
    # is absent here. `-p` is the value-taking prompt flag (report §2), `-m` the model alias.
    _emitted_flags: tuple[str, ...] = ("-p", "-m", "--output-format")

    # NOTE: no `_model_findings` override (inherits the no-op default), same as codex — kimi has no
    # queryable model roster (report §10: `kimi --help` lists no `models` subcommand), and under
    # this design cairn always emits `-m cairn` against its OWN self-written alias, so a `--help`-
    # scraping roster check would be moot. A bad provider/model errors loudly at run time.

    def _extra_env(self, inv: Invocation) -> dict[str, str]:
        """Write the throwaway per-INVOCATION ``KIMI_CODE_HOME`` (config isolation + effort,
        report §6) and hand kimi the env that points at it plus the CI-hygiene knobs.

        This is kimi's ONLY config-isolation channel: there is no ``--ignore-user-config`` flag, so
        relocating the whole data root to a fresh dir is what makes identical runs deterministic
        (no ambient ``~/.kimi-code`` config leaks in). The config names the provider's key ENV VAR —
        never its value (brief rule 4); cairn already carries that value in ``inv.env``.

        Keyed on ``inv.log_path.stem`` (unique per step+attempt+cycle, walk.py's ``_log_path``),
        NOT on ``inv.cwd`` alone: ``inv.cwd`` is the run dir SHARED by every step in a run
        (walk.py sets ``cwd=self.run_dir`` unconditionally), and a ``ParallelNode`` runs its
        children concurrently on the SAME executor instance (``ThreadPoolExecutor``). Deriving the
        home from ``inv.cwd`` alone let two parallel kimi steps with different model/effort race to
        write the same ``config.toml`` — one step's model/effort would silently win for BOTH
        (xcli-review-quality.md H1). Keying per-invocation also means a retried attempt gets a
        FRESH home rather than reusing another attempt's stale generated state.
        """
        home = Path(inv.cwd) / ".cairn" / f"kimi-home.{inv.log_path.stem}"
        home.mkdir(parents=True, exist_ok=True)
        provider_type = self.config.flags.get("provider_type", _DEFAULT_PROVIDER_TYPE)
        api_key_env = self.config.flags.get("api_key_env", _DEFAULT_API_KEY_ENV)
        effort = map_effort(inv.effort) if inv.effort is not None else None
        (home / "config.toml").write_text(
            render_config_toml(
                provider_type=provider_type,
                api_key_env=api_key_env,
                model=inv.model,
                effort=effort,
            ),
            encoding="utf-8",
        )
        return {
            "KIMI_CODE_HOME": str(home),
            # CI hygiene (report §11). Auto-update would block/prompt in a headless run; telemetry
            # off by policy; KIMI_DISABLE_CRON stops a step leaving a recurring scheduled task
            # behind it (a degree of freedom the FS sandbox does not otherwise constrain — §11).
            "KIMI_CODE_NO_AUTO_UPDATE": "1",
            "KIMI_DISABLE_TELEMETRY": "1",
            "KIMI_DISABLE_CRON": "1",
        }

    def _build_command(self, inv: Invocation, prompt_text: str) -> tuple[list[str], str | None]:
        # re-verify against `kimi --help` at doctor time; vendors drift.
        argv = [
            "kimi",
            # [UNC] Prompt on argv. `-p, --prompt <prompt>` is a value-taking flag; whether `-p`
            # can read the prompt from stdin instead is UNDOCUMENTED for kimi-code (report §2/§12
            # risk 1), unlike claude's `-p` stdin fallback. argv is the safe, documented channel.
            # FUTURE: if a live probe confirms `-p` + piped stdin works, deliver the envelope on
            # stdin and return stdin_text=prompt_text — that keeps a skill-heavy envelope off
            # `ps`/`/proc/*/cmdline` and off the argv length ceiling (the reason claude is stdin).
            "-p", prompt_text,
            # [DECISION §3/§6] Always the self-written alias in the per-run KIMI_CODE_HOME config
            # (see _extra_env). It carries model + mapped reasoning_effort, since kimi has no
            # per-invocation flag for either.
            "-m", "cairn",
            # [DOC §8] `text` (default, made explicit): directly comparable to claude/codex plain
            # text, and sentinel-safe (§2). `stream-json` is the alternative but is not wired.
            "--output-format", "text",
        ]
        # NOTE (network): inv.network is NOT consumed here. kimi exposes no per-step network toggle
        # (report §10) — a `network:false` step has nothing on the kimi side to enforce it; the OS
        # FS sandbox is the only containment layer, and it keeps network ON (fs posture). Not a
        # silent drop — there is simply no kimi lever to wire, same shape as grok.
        return argv, None  # prompt on argv (see the [UNC] note above); stdin is not used
