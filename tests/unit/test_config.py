"""Load + validate cairn.toml into typed config; precise errors on malformed input."""

from __future__ import annotations

from pathlib import Path

import pytest

from cairn.kernel.config import Config, load_config, version_compat
from cairn.kernel.errors import ConfigError

FIXTURES = Path(__file__).parent / "fixtures"
GOOD = FIXTURES / "good-workspace"


def test_load_good_workspace_returns_typed_config():
    cfg = load_config(GOOD)
    assert isinstance(cfg, Config)
    assert cfg.workspace.name == "brease-factory"
    assert cfg.workspace.doctrine == "prompts/DOCTRINE.md"
    assert cfg.workspace.runs_dir == "runs"
    assert cfg.workspace.default_executor == "claude"


def test_defaults_parse_durations_and_nested_tables():
    cfg = load_config(GOOD)
    assert cfg.defaults.step_timeout_s == 1800  # "30m"
    assert cfg.defaults.heartbeat_s == 20       # "20s"
    assert cfg.defaults.trail_context.events == 12
    assert cfg.defaults.trail_context.learnings == 5
    assert cfg.defaults.budget is not None
    assert cfg.defaults.budget.run_usd == 25
    assert cfg.defaults.budget.step_usd == 8


def test_executor_tiers_and_flags():
    cfg = load_config(GOOD)
    claude = cfg.executors["claude"]
    assert claude.enabled is True
    assert claude.tiers["reasoning"].model == "opus"
    assert claude.tiers["reasoning"].effort == "high"

    codex = cfg.executors["codex"]
    assert codex.pin_version == "0.138"
    assert codex.flags["sandbox"] == "workspace-write"

    grok = cfg.executors["grok"]
    assert grok.setup == "scripts/setup-grok-config.sh"
    # Grok aliases bake effort — no effort key on the tier.
    assert grok.tiers["reasoning"].model == "grok-4.3-high"
    assert grok.tiers["reasoning"].effort is None


def test_tools_and_secrets_scope_to_steps():
    cfg = load_config(GOOD)
    assert cfg.tools["vercel"].check == "vercel whoami"
    assert cfg.tools["vercel"].needed_by == ["deploy"]
    assert cfg.tools["crawl4ai"].needed_by == []  # omitted → empty
    assert cfg.secrets["BREASE_TOKEN"].needed_by == ["model-cms", "populate"]
    assert cfg.sinks["webhook"]["url"] == "https://example.invalid/hook"


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)
    assert "cairn.toml" in str(exc.value)


def test_bad_toml_raises_config_error(tmp_path):
    (tmp_path / "cairn.toml").write_text("this is = = not toml", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_bad_tier_name_raises_config_error_naming_the_tier(tmp_path):
    (tmp_path / "cairn.toml").write_text(
        "[workspace]\n"
        'name = "w"\n'
        "[executors.claude.tiers]\n"
        'genius = { model = "opus" }\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)
    assert "genius" in str(exc.value) or any(
        "genius" in f.message for f in exc.value.findings
    )


def test_unknown_top_level_key_is_a_warning_not_an_error(tmp_path):
    (tmp_path / "cairn.toml").write_text(
        "[workspace]\n" 'name = "w"\n' "[mystery]\n" "foo = 1\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert any(f.level == "warning" and "mystery" in f.message for f in cfg.warnings)


# ---- value-type validation (review MINOR 1) ----


def test_bad_budget_value_type_raises(tmp_path):
    (tmp_path / "cairn.toml").write_text(
        '[workspace]\nname = "w"\n[defaults.budget]\nrun_usd = "lots"\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)
    assert "run_usd" in str(exc.value)


def test_bad_trail_context_value_type_raises(tmp_path):
    (tmp_path / "cairn.toml").write_text(
        '[workspace]\nname = "w"\n[defaults]\ntrail_context = { events = "twelve" }\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)
    assert "events" in str(exc.value)


def test_bad_executor_enabled_type_raises(tmp_path):
    (tmp_path / "cairn.toml").write_text(
        '[workspace]\nname = "w"\n'
        '[executors.claude]\nenabled = "yes"\n'
        '[executors.claude.tiers]\nreasoning = { model = "opus" }\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)
    assert "enabled" in str(exc.value)


# ---- load-error findings are never empty (review MINOR 2) ----


def test_missing_file_error_carries_a_finding(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)
    assert exc.value.findings
    assert exc.value.findings[0].level == "error"


def test_bad_toml_error_carries_a_finding(tmp_path):
    (tmp_path / "cairn.toml").write_text("this is = = not toml", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)
    assert exc.value.findings
    assert exc.value.findings[0].level == "error"


# ---- nested unknown-key warnings (review MINOR 3) ----


def test_good_workspace_produces_no_warnings():
    assert load_config(GOOD).warnings == []


def test_unknown_key_in_workspace_warns(tmp_path):
    (tmp_path / "cairn.toml").write_text(
        '[workspace]\nname = "w"\nnmae = "typo"\n', encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert any(f.level == "warning" and "nmae" in f.message for f in cfg.warnings)


def test_unknown_key_in_defaults_warns(tmp_path):
    (tmp_path / "cairn.toml").write_text(
        '[workspace]\nname = "w"\n[defaults]\nstep_tmeout = "30m"\n', encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert any(f.level == "warning" and "step_tmeout" in f.message for f in cfg.warnings)


def test_unknown_key_in_tier_warns(tmp_path):
    (tmp_path / "cairn.toml").write_text(
        '[workspace]\nname = "w"\n'
        '[executors.claude.tiers]\nreasoning = { model = "opus", efort = "high" }\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert any(f.level == "warning" and "efort" in f.message for f in cfg.warnings)


def test_unknown_key_in_executor_warns(tmp_path):
    (tmp_path / "cairn.toml").write_text(
        '[workspace]\nname = "w"\n'
        '[executors.claude]\nenbaled = true\n'
        '[executors.claude.tiers]\nreasoning = { model = "opus" }\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert any(f.level == "warning" and "enbaled" in f.message for f in cfg.warnings)


# --------------------------------------------------------------------------- #
# The `requires` version pin — the matching rule (docs/DISTRIBUTION.md §3).
# PEP 440 subset, stdlib-only: comma-AND clauses; ==/!=/>=/<=/>/</~= plus the
# ==X.Y.* prefix wildcard; releases compare zero-padded; dev/pre sort before
# their release; a bare clause means exact equality.
# --------------------------------------------------------------------------- #

from cairn.kernel.config import check_requires, requires_satisfied


@pytest.mark.parametrize(
    ("spec", "installed", "ok"),
    [
        # the canonical DISTRIBUTION §3 pin
        (">=0.1,<0.2", "0.1.0", True),
        (">=0.1,<0.2", "0.1.9", True),
        (">=0.1,<0.2", "0.2.0", False),
        (">=0.1,<0.2", "0.0.9", False),
        # zero-padded release comparison
        (">=0.1", "0.1", True),
        ("==0.1", "0.1.0", True),
        # bare clause = exact equality
        ("0.1.0", "0.1.0", True),
        ("0.1.0", "0.1.1", False),
        # prefix wildcard + compatible-release
        ("==0.1.*", "0.1.7", True),
        ("==0.1.*", "0.2.0", False),
        ("~=0.1.0", "0.1.4", True),
        ("~=0.1.0", "0.2.0", False),
        # dev/pre-releases order BEFORE their release (PEP 440)
        (">=0.1", "0.1.0.dev0", False),
        (">=0.1", "0.1.0rc1", False),
        ("<0.2", "0.2.0.dev0", True),
        ("!=0.1.0", "0.1.0", False),
        ("!=0.1.0", "0.1.1", True),
    ],
)
def test_requires_satisfied_matching_rule(spec, installed, ok):
    assert requires_satisfied(spec, installed) is ok


@pytest.mark.parametrize("spec", ["", " , ", ">=banana", "~=1", ">=0.1.*"])
def test_requires_satisfied_rejects_malformed_specs(spec):
    with pytest.raises(ValueError):
        requires_satisfied(spec, "0.1.0")


def test_check_requires_names_both_versions(tmp_path):
    with pytest.raises(ConfigError) as exc:
        check_requires(">=9.0", file=tmp_path / "cairn.toml", installed="0.1.0")
    msg = str(exc.value)
    assert ">=9.0" in msg and "0.1.0" in msg


def test_check_requires_no_pin_is_a_noop(tmp_path):
    check_requires(None, file=tmp_path / "cairn.toml", installed="0.1.0")


# --------------------------------------------------------------------------- #
# version_compat — the cross-version resume gate's classifier (DISTRIBUTION §3).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("recorded", "installed", "verdict"),
    [
        # same release / patch-only drift within a minor ⇒ silent
        ("0.1.0", "0.1.0", "ok"),
        ("0.1.0", "0.1.9", "ok"),
        ("0.1", "0.1.4", "ok"),
        # pre/dev tags compare by release tuple only — same major.minor ⇒ silent
        ("0.2.0rc1", "0.2.0", "ok"),
        ("0.2.0.dev1", "0.2.5", "ok"),
        # cross-minor within the same major ⇒ warn, never refuse
        ("0.1.0", "0.2.0", "warn"),
        ("0.9.0", "0.1.0", "warn"),
        # cross-major ⇒ refuse (only --force gets through)
        ("1.0.0", "0.1.0", "refuse"),
        ("9.0.0", "0.1.0", "refuse"),
        ("0.1.0", "1.4.0", "refuse"),
        # unrecorded (legacy) or unparseable ⇒ warn and proceed
        (None, "0.1.0", "warn"),
        ("", "0.1.0", "warn"),
        ("banana", "0.1.0", "warn"),
    ],
)
def test_version_compat_classifies(recorded, installed, verdict):
    assert version_compat(recorded, installed) == verdict
