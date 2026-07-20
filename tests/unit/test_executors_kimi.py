"""KimiExecutor — prompt on argv, per-run KIMI_CODE_HOME config isolation, effort baked into a
self-written model alias, sandbox posture fs."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from cairn.kernel.sandbox import NativeBackend

from cairn.executors.kimi import KimiExecutor, map_effort, render_config_toml
from cairn.kernel.config import ExecutorConfig, TierSpec
from cairn.kernel.errors import ConfigError
from cairn.kernel.types import Capabilities

from test_executors_base import fake_env, make_inv, read_scratch, use_fakebin

CFG = ExecutorConfig(
    name="kimi",
    pin_version="0.28",
    tiers={
        "reasoning": TierSpec(model="k3", effort="high"),
        "cheap": TierSpec(model="kimi-for-coding"),
    },
    flags={"provider_type": "anthropic", "api_key_env": "CAIRN_KIMI_PROVIDER_KEY"},
)


def _invoke(tmp_path, monkeypatch, **kw):
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    kw.setdefault("model", "k3")
    inv = make_inv(tmp_path, scratch=scratch, **kw)
    result = KimiExecutor(CFG).invoke(inv)
    argv, env, stdin = read_scratch(scratch)
    return result, inv, argv, env, stdin


def _kimi_home(inv) -> Path:
    """The per-invocation KIMI_CODE_HOME the executor derives (keyed on log_path.stem — H1)."""
    return inv.cwd / ".cairn" / f"kimi-home.{inv.log_path.stem}"


def _kimi_home_config(inv) -> dict:
    """Parse the config.toml the executor wrote under the per-invocation KIMI_CODE_HOME."""
    path = _kimi_home(inv) / "config.toml"
    return tomllib.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# argv shape + prompt delivery
# --------------------------------------------------------------------------- #


def test_argv_shape_and_prompt_on_argv(tmp_path, monkeypatch):
    _, _, argv, _, stdin = _invoke(tmp_path, monkeypatch, prompt="the kimi prompt", effort="high")
    assert argv[0].split("/")[-1] == "kimi"  # argv[0] is the PATH-resolved binary
    assert argv[1:] == [
        "-p", "the kimi prompt",
        "-m", "cairn",
        "--output-format", "text",
    ]
    assert stdin == ""  # kimi gets the prompt on argv, not stdin (stdin_text=None)


def test_prompt_is_on_argv_not_stdin(tmp_path, monkeypatch):
    # Explicit inverse of the claude/codex stdin contract: the envelope IS on the ps surface here
    # (the [UNC] tradeoff in _build_command — stdin fallback is undocumented for kimi).
    _, _, argv, _, stdin = _invoke(tmp_path, monkeypatch, prompt="ENVELOPE-BODY")
    assert "ENVELOPE-BODY" in argv
    assert stdin == ""


def test_always_selects_the_self_written_alias(tmp_path, monkeypatch):
    # kimi has no per-invocation effort flag; effort + model ride the `cairn` alias in the per-run
    # config, so argv always pins `-m cairn`, never the raw tier model.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, model="k3", effort="high")
    i = argv.index("-m")
    assert argv[i + 1] == "cairn"
    assert "k3" not in argv  # the concrete model is in config.toml, not argv


def test_output_format_text(tmp_path, monkeypatch):
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch)
    i = argv.index("--output-format")
    assert argv[i + 1] == "text"


def test_bullet_prefixed_sentinel_still_parses(tmp_path, monkeypatch):
    # `text` mode prefixes the sentinel line with `• ` — parse_step_sentinel scans for `<<<STEP`
    # un-anchored (report §2), so the bullet is harmless.
    result, _, _, _, _ = _invoke(tmp_path, monkeypatch)
    assert result.step == {"status": "done", "summary": "fake ok", "artifacts": []}


# --------------------------------------------------------------------------- #
# per-run KIMI_CODE_HOME config isolation + env hygiene
# --------------------------------------------------------------------------- #


def test_kimi_code_home_points_under_run_dir(tmp_path, monkeypatch):
    _, inv, _, env, _ = _invoke(tmp_path, monkeypatch)
    expected = _kimi_home(inv)
    assert env["KIMI_CODE_HOME"] == str(expected)
    assert (expected / "config.toml").is_file()


def test_parallel_steps_get_isolated_homes_with_own_config(tmp_path, monkeypatch):
    # H1 (xcli-review-quality.md): a ParallelNode runs its children concurrently on the SAME
    # executor instance, and walk.py sets inv.cwd = self.run_dir for every step — so two parallel
    # kimi steps with different model/effort share one cwd exactly like this. Before the fix,
    # _extra_env derived KIMI_CODE_HOME from inv.cwd alone, so BOTH invocations below would
    # resolve to the identical `.cairn/kimi-home` dir; invoking B second would overwrite A's
    # config.toml in place, leaving env_a["KIMI_CODE_HOME"] == env_b["KIMI_CODE_HOME"] and BOTH
    # captured configs showing step B's model/effort. This test fails on that old code — the very
    # first assertion (distinct KIMI_CODE_HOME paths) does not hold, and the config assertions
    # for step A would read step B's model ("kimi-for-coding"/"max") instead of its own.
    use_fakebin(monkeypatch)
    scratch_a = tmp_path / "scratch-a"
    scratch_b = tmp_path / "scratch-b"
    inv_a = make_inv(
        tmp_path, scratch=scratch_a, model="k3", effort="low",
        log_path=tmp_path / "logs" / "stepA.log",
    )
    inv_b = make_inv(
        tmp_path, scratch=scratch_b, model="kimi-for-coding", effort="max",
        log_path=tmp_path / "logs" / "stepB.log",
    )
    assert inv_a.cwd == inv_b.cwd  # both steps of one run share the run dir (walk.py:471)

    ex = KimiExecutor(CFG)
    ex.invoke(inv_a)
    ex.invoke(inv_b)

    _, env_a, _ = read_scratch(scratch_a)
    _, env_b, _ = read_scratch(scratch_b)
    assert env_a["KIMI_CODE_HOME"] != env_b["KIMI_CODE_HOME"]  # distinct homes, no shared-file race

    cfg_a = tomllib.loads((Path(env_a["KIMI_CODE_HOME"]) / "config.toml").read_text())
    cfg_b = tomllib.loads((Path(env_b["KIMI_CODE_HOME"]) / "config.toml").read_text())
    assert cfg_a["models"]["cairn"]["model"] == "k3"
    assert cfg_a["models"]["cairn"]["overrides"]["reasoning_effort"] == "low"
    assert cfg_b["models"]["cairn"]["model"] == "kimi-for-coding"
    assert cfg_b["models"]["cairn"]["overrides"]["reasoning_effort"] == "max"


def test_ci_hygiene_env_set(tmp_path, monkeypatch):
    # report §11: auto-update off (would block/prompt headless), telemetry off, cron off (a step
    # must not leave a recurring scheduled task behind it).
    _, _, _, env, _ = _invoke(tmp_path, monkeypatch)
    assert env["KIMI_CODE_NO_AUTO_UPDATE"] == "1"
    assert env["KIMI_DISABLE_TELEMETRY"] == "1"
    assert env["KIMI_DISABLE_CRON"] == "1"


def test_config_defines_cairn_alias(tmp_path, monkeypatch):
    _, inv, _, _, _ = _invoke(tmp_path, monkeypatch, model="k3", effort="high")
    cfg = _kimi_home_config(inv)
    assert cfg["default_model"] == "cairn"
    assert cfg["providers"]["cairn"]["type"] == "anthropic"  # from flags
    assert cfg["models"]["cairn"]["provider"] == "cairn"
    assert cfg["models"]["cairn"]["model"] == "k3"


def test_config_names_key_env_var_never_the_secret(tmp_path, monkeypatch):
    # brief rule 4: the API KEY VALUE must never touch disk — config.toml only names the env var
    # kimi reads it from. Plant the secret's value in inv.env and assert it is absent from config.
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    env = fake_env(scratch, CAIRN_KIMI_PROVIDER_KEY="sk-super-secret-VALUE")
    inv = make_inv(tmp_path, scratch=scratch, model="k3", env=env)
    KimiExecutor(CFG).invoke(inv)
    config_text = (_kimi_home(inv) / "config.toml").read_text()
    assert 'api_key = "CAIRN_KIMI_PROVIDER_KEY"' in config_text  # the NAME
    assert "sk-super-secret-VALUE" not in config_text  # never the value


def test_config_provider_and_key_env_defaults(tmp_path, monkeypatch):
    # With no flags, fall back to the documented defaults (provider `kimi`, key var KIMI_API_KEY).
    cfg = ExecutorConfig(name="kimi", tiers={"cheap": TierSpec(model="k3")})
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    inv = make_inv(tmp_path, scratch=scratch, model="k3", effort=None)
    KimiExecutor(cfg).invoke(inv)
    parsed = tomllib.loads((_kimi_home(inv) / "config.toml").read_text())
    assert parsed["providers"]["cairn"]["type"] == "kimi"
    assert parsed["providers"]["cairn"]["env"]["api_key"] == "KIMI_API_KEY"


# --------------------------------------------------------------------------- #
# effort mapping (cairn 5-value → kimi 3-value), baked into config, not argv
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cairn_effort, kimi_effort",
    [("low", "low"), ("medium", "high"), ("high", "high"), ("xhigh", "max"), ("max", "max")],
)
def test_map_effort_table(cairn_effort, kimi_effort):
    # The documented table (report §3): kimi has no middle rung, so medium maps UP to high.
    assert map_effort(cairn_effort) == kimi_effort


def test_map_effort_unmappable_falls_back_to_high(tmp_path):
    # Never a crash (brief rule 5) — an unexpected string degrades to kimi's vendor default.
    assert map_effort("nonsense") == "high"


def test_effort_baked_into_config_not_argv(tmp_path, monkeypatch):
    _, inv, argv, _, _ = _invoke(tmp_path, monkeypatch, effort="xhigh")
    assert "--effort" not in argv  # kimi has no effort flag
    cfg = _kimi_home_config(inv)
    assert cfg["models"]["cairn"]["overrides"]["reasoning_effort"] == "max"  # xhigh → max


def test_effort_medium_maps_up_to_high_in_config(tmp_path, monkeypatch):
    _, inv, _, _, _ = _invoke(tmp_path, monkeypatch, effort="medium")
    cfg = _kimi_home_config(inv)
    assert cfg["models"]["cairn"]["overrides"]["reasoning_effort"] == "high"


def test_effort_none_omits_overrides_table(tmp_path, monkeypatch):
    _, inv, _, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    cfg = _kimi_home_config(inv)
    assert "overrides" not in cfg["models"]["cairn"]


def test_render_config_toml_is_valid_toml_with_effort():
    # Direct unit test of the hand-rolled emitter: round-trips through tomllib.
    text = render_config_toml(
        provider_type="openai", api_key_env="MY_KEY", model="k3", effort="max"
    )
    parsed = tomllib.loads(text)
    assert parsed == {
        "default_model": "cairn",
        "providers": {"cairn": {"type": "openai", "env": {"api_key": "MY_KEY"}}},
        "models": {"cairn": {"provider": "cairn", "model": "k3", "overrides": {"reasoning_effort": "max"}}},
    }


def test_render_config_toml_escapes_quotes():
    # A double-quote in a value must not break (or widen) the emitted TOML.
    text = render_config_toml(
        provider_type='we"ird', api_key_env="K", model="m", effort=None
    )
    assert tomllib.loads(text)["providers"]["cairn"]["type"] == 'we"ird'


def test_render_config_toml_escapes_newline_and_carriage_return():
    # L1 (xcli-review-quality.md): a raw newline in a value would either break the single-line
    # TOML basic-string grammar or smuggle in a new key/table row. Escaped, not rejected, and
    # still round-trips to the ORIGINAL string value once parsed.
    text = render_config_toml(
        provider_type="line1\nline2\r\nline3", api_key_env="K", model="m", effort=None
    )
    assert tomllib.loads(text)["providers"]["cairn"]["type"] == "line1\nline2\r\nline3"
    # And the raw bytes on the wire never contain an actual newline inside the quoted string —
    # each line of the rendered TOML is one of the declared table lines, nothing extra.
    for line in text.splitlines():
        if line.startswith("type ="):
            assert line.count('"') == 2  # exactly one opening + one closing quote, unbroken


# --------------------------------------------------------------------------- #
# env sealing + cwd
# --------------------------------------------------------------------------- #


def test_env_passed_exactly(tmp_path, monkeypatch):
    _, _, _, env, _ = _invoke(tmp_path, monkeypatch)
    assert env["CAIRN_CANARY"] == "canary-value"
    assert "OS_ONLY_SECRET" not in env  # os.environ was not inherited


def test_cwd_is_the_run_dir(tmp_path, monkeypatch):
    _, inv, _, _, _ = _invoke(tmp_path, monkeypatch)
    assert (tmp_path / "scratch" / "cwd.txt").read_text() == str(inv.cwd)


def test_step_parsed_and_exit_code(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    inv = make_inv(tmp_path, scratch=scratch, model="k3", env=fake_env(scratch, CAIRN_TEST_EXIT="4"))
    result = KimiExecutor(CFG).invoke(inv)
    assert result.exit_code == 4
    assert result.step == {"status": "done", "summary": "fake ok", "artifacts": []}


# --------------------------------------------------------------------------- #
# capabilities + model resolution
# --------------------------------------------------------------------------- #


def test_capabilities():
    caps = KimiExecutor(CFG).capabilities
    # blocking_hooks None (headless firing UNVERIFIED + fail-open — doctor probes later);
    # output_schema False (STEP sentinel is the contract); session_capture None (structural, via
    # throwaway KIMI_CODE_HOME); installs_hooks False; sandbox fs (OS FS-wrap, same as claude).
    assert caps == Capabilities(
        blocking_hooks=None, output_schema=False, session_capture=None,
        installs_hooks=False, sandbox="fs",
    )


def test_resolve_model_fixed_and_passthrough_effort():
    ex = KimiExecutor(CFG)
    assert ex.resolve_model("reasoning", "low") == ("k3", "high")  # tier fixes effort
    assert ex.resolve_model("cheap", "medium") == ("kimi-for-coding", "medium")  # passthrough


def test_resolve_model_unknown_tier_raises():
    with pytest.raises(ConfigError):
        KimiExecutor(CFG).resolve_model("no-such-tier", "high")


def test_render_workspace_writes_agents_md(tmp_path):
    from types import SimpleNamespace

    doctrine = tmp_path / "DOCTRINE.md"
    doctrine.write_text("kimi doctrine", encoding="utf-8")
    KimiExecutor(CFG).render_workspace(SimpleNamespace(root=tmp_path, doctrine=doctrine))
    assert "kimi doctrine" in (tmp_path / "AGENTS.md").read_text()
    assert not (tmp_path / "CLAUDE.md").exists()


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def test_doctor_healthy_with_fake_version(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)  # fake prints "kimi 0.28.0" → satisfies pin 0.28
    # Hermetic: stub the sandbox primitive as present so this asserts doctor's logic on a healthy
    # machine, not whether the host has sandbox-exec/bwrap (the fs-posture WARN branch is covered
    # hermetically in test_sandbox.py).
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    assert KimiExecutor(CFG).doctor() == []


def test_doctor_reports_missing_binary(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    findings = KimiExecutor(CFG).doctor()
    assert any(f.level == "error" and f.fix for f in findings)


def test_doctor_warns_on_pin_mismatch(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("CAIRN_TEST_VERSION", "kimi 0.99.0 (fake)")
    findings = KimiExecutor(CFG).doctor()
    assert any(f.level == "warning" and "0.28" in f.message for f in findings)


# --------------------------------------------------------------------------- #
# W5b — doctor flag-drift checks
# --------------------------------------------------------------------------- #


def test_doctor_warns_when_emitted_flag_missing_from_help(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("CAIRN_TEST_HELP_OMIT", "--output-format")
    findings = KimiExecutor(CFG).doctor()
    assert any(
        f.level == "warning" and "--output-format" in f.message and "not advertised" in f.message
        for f in findings
    )


def test_doctor_no_flag_warning_on_healthy_help(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    findings = KimiExecutor(CFG).doctor()
    assert not any("not advertised" in f.message for f in findings)


def test_doctor_warns_when_help_fetch_fails(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("CAIRN_TEST_HELP_FAIL", "1")
    findings = KimiExecutor(CFG).doctor()
    assert any(f.level == "warning" and "could not run" in f.message for f in findings)
    assert sum("not advertised" in f.message for f in findings) == 0


def test_doctor_never_hard_fails_on_flag_drift(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("CAIRN_TEST_HELP_OMIT", "-p,-m,--output-format")
    findings = KimiExecutor(CFG).doctor()
    assert findings  # something warned
    assert not any(f.level == "error" for f in findings)
