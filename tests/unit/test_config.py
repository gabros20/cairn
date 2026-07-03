"""Load + validate cairn.toml into typed config; precise errors on malformed input."""

from __future__ import annotations

from pathlib import Path

import pytest

from cairn.kernel.config import Config, load_config
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
