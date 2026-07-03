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
