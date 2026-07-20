"""HermesExecutor — prompt via `-z` argv (not stdin), config isolation, effort via per-run
HERMES_HOME/config.yaml, fs sandbox posture, API-key auth advisory."""

from __future__ import annotations

from pathlib import Path

import pytest

from cairn.executors.hermes import HermesExecutor, _EFFORT_MAP, _map_effort
from cairn.kernel.config import ExecutorConfig, TierSpec
from cairn.kernel.errors import ConfigError
from cairn.kernel.sandbox import NativeBackend
from cairn.kernel.types import Capabilities

from test_executors_base import fake_env, make_inv, read_scratch, use_fakebin


def _hermes_home(inv) -> Path:
    """The per-invocation HERMES_HOME the executor derives (keyed on log_path.stem — H1)."""
    return inv.cwd / ".cairn" / f"hermes-home.{inv.log_path.stem}"


CFG = ExecutorConfig(
    name="hermes",
    pin_version="0.18",
    tiers={
        "reasoning": TierSpec(model="anthropic/claude-opus-4.8", effort="high"),
        "cheap": TierSpec(model="anthropic/claude-haiku-4.5"),
    },
)


def _invoke(tmp_path, monkeypatch, **kw):
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    inv = make_inv(tmp_path, scratch=scratch, model="anthropic/claude-opus-4.8", **kw)
    result = HermesExecutor(CFG).invoke(inv)
    argv, env, stdin = read_scratch(scratch)
    return result, inv, argv, env, stdin


# --------------------------------------------------------------------------- #
# argv shape + prompt delivery
# --------------------------------------------------------------------------- #


def test_argv_shape_and_prompt_on_argv_with_effort(tmp_path, monkeypatch):
    _, inv, argv, _, stdin = _invoke(tmp_path, monkeypatch, prompt="the hermes prompt", effort="high")
    assert argv[0].split("/")[-1] == "hermes"
    usage = str(inv.cwd / ".cairn" / "hermes-usage.json")
    # effort set → NO --ignore-user-config (the clean-room HERMES_HOME is the isolation instead).
    assert argv[1:] == [
        "-z", "the hermes prompt",
        "-m", "anthropic/claude-opus-4.8",
        "--ignore-rules", "--accept-hooks", "--yolo",
        "--usage-file", usage,
    ]
    assert stdin == ""  # prompt rides argv (-z), hermes -z reads no stdin


def test_ignore_user_config_present_when_effort_none(tmp_path, monkeypatch):
    # effort None → no per-run HERMES_HOME, so --ignore-user-config seals the default ~/.hermes.
    _, inv, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    usage = str(inv.cwd / ".cairn" / "hermes-usage.json")
    assert argv[1:] == [
        "-z", "do the thing",
        "-m", "anthropic/claude-opus-4.8",
        "--ignore-user-config",
        "--ignore-rules", "--accept-hooks", "--yolo",
        "--usage-file", usage,
    ]


def test_ignore_user_config_absent_when_effort_set(tmp_path, monkeypatch):
    # Emitting --ignore-user-config alongside a relocated HERMES_HOME would ignore the config.yaml
    # carrying reasoning_effort — so it is omitted whenever effort is delivered via the home.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort="high")
    assert "--ignore-user-config" not in argv


def test_config_isolation_and_defense_flags_present(tmp_path, monkeypatch):
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    for flag in ("--ignore-rules", "--accept-hooks", "--yolo"):
        assert flag in argv


def test_usage_file_under_run_dir(tmp_path, monkeypatch):
    # Observability sidecar under the run dir — never load-bearing for the Result.
    _, inv, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    i = argv.index("--usage-file")
    assert argv[i + 1] == str(inv.cwd / ".cairn" / "hermes-usage.json")


# --------------------------------------------------------------------------- #
# effort → per-run HERMES_HOME/config.yaml (novel to hermes)
# --------------------------------------------------------------------------- #


def test_effort_generates_hermes_home_with_config(tmp_path, monkeypatch):
    _, inv, _, env, _ = _invoke(tmp_path, monkeypatch, effort="high")
    home = _hermes_home(inv)
    assert env["HERMES_HOME"] == str(home)  # child process saw the relocated home
    cfg = (home / "config.yaml").read_text(encoding="utf-8")
    assert cfg == "agent:\n  reasoning_effort: high\n"


def test_effort_none_sets_no_hermes_home(tmp_path, monkeypatch):
    _, inv, _, env, _ = _invoke(tmp_path, monkeypatch, effort=None)
    assert "HERMES_HOME" not in env
    assert not _hermes_home(inv).exists()


def test_hermes_home_generation_is_idempotent(tmp_path, monkeypatch):
    # A second identical invocation (SAME log_path stem — a retried attempt, not a parallel
    # sibling) must not error and must leave byte-identical config.
    _invoke(tmp_path, monkeypatch, effort="high")
    _, inv, _, _, _ = _invoke(tmp_path, monkeypatch, effort="high")
    cfg = (_hermes_home(inv) / "config.yaml").read_text(encoding="utf-8")
    assert cfg == "agent:\n  reasoning_effort: high\n"


def test_parallel_steps_get_isolated_homes_with_own_effort(tmp_path, monkeypatch):
    # H1 (xcli-review-quality.md): a ParallelNode runs its children concurrently on the SAME
    # executor instance, and walk.py sets inv.cwd = self.run_dir for every step — so two parallel
    # hermes steps with different effort share one cwd exactly like this. Before the fix,
    # _extra_env derived HERMES_HOME from inv.cwd alone, so BOTH invocations below would resolve
    # to the identical `.cairn/hermes-home` dir; invoking B second would overwrite A's config.yaml
    # in place, leaving env_a["HERMES_HOME"] == env_b["HERMES_HOME"] and BOTH captured configs
    # showing step B's effort ("low") instead of their own. This test fails on that old code.
    use_fakebin(monkeypatch)
    scratch_a = tmp_path / "scratch-a"
    scratch_b = tmp_path / "scratch-b"
    inv_a = make_inv(
        tmp_path, scratch=scratch_a, model="anthropic/claude-opus-4.8", effort="high",
        log_path=tmp_path / "logs" / "stepA.log",
    )
    inv_b = make_inv(
        tmp_path, scratch=scratch_b, model="anthropic/claude-haiku-4.5", effort="low",
        log_path=tmp_path / "logs" / "stepB.log",
    )
    assert inv_a.cwd == inv_b.cwd  # both steps of one run share the run dir (walk.py:471)

    ex = HermesExecutor(CFG)
    ex.invoke(inv_a)
    ex.invoke(inv_b)

    _, env_a, _ = read_scratch(scratch_a)
    _, env_b, _ = read_scratch(scratch_b)
    assert env_a["HERMES_HOME"] != env_b["HERMES_HOME"]  # distinct homes, no shared-file race

    cfg_a = (Path(env_a["HERMES_HOME"]) / "config.yaml").read_text(encoding="utf-8")
    cfg_b = (Path(env_b["HERMES_HOME"]) / "config.yaml").read_text(encoding="utf-8")
    assert cfg_a == "agent:\n  reasoning_effort: high\n"
    assert cfg_b == "agent:\n  reasoning_effort: low\n"


def test_no_secret_written_into_hermes_home(tmp_path, monkeypatch):
    # brief rule 4: auth stays env-side, NEVER on disk. The generated home holds only config.yaml.
    _, inv, _, _, _ = _invoke(tmp_path, monkeypatch, effort="high")
    home = _hermes_home(inv)
    assert {p.name for p in home.iterdir()} == {"config.yaml"}
    assert "leak-me" not in (home / "config.yaml").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# effort mapping table (pure function)
# --------------------------------------------------------------------------- #


def test_effort_map_passthrough_low_medium_high():
    assert _map_effort("low") == "low"
    assert _map_effort("medium") == "medium"
    assert _map_effort("high") == "high"


def test_effort_map_folds_xhigh_and_max_to_high():
    # hermes has no xhigh/max reasoning tier — nearest known value is high.
    assert _map_effort("xhigh") == "high"
    assert _map_effort("max") == "high"


def test_effort_map_unmappable_is_nearest_never_crash():
    # An effort outside cairn's EFFORTS never crashes — folds to the neutral medium default.
    assert _map_effort("bananas") == "medium"


def test_effort_map_covers_every_cairn_effort():
    from cairn.kernel.types import EFFORTS

    assert set(_EFFORT_MAP) == set(EFFORTS)
    assert all(_map_effort(e) in {"low", "medium", "high"} for e in EFFORTS)


def test_effort_xhigh_bakes_high_into_config(tmp_path, monkeypatch):
    _, inv, _, _, _ = _invoke(tmp_path, monkeypatch, effort="xhigh")
    cfg = (_hermes_home(inv) / "config.yaml").read_text(encoding="utf-8")
    assert cfg == "agent:\n  reasoning_effort: high\n"


# --------------------------------------------------------------------------- #
# env sealing + cwd
# --------------------------------------------------------------------------- #


def test_env_passed_exactly_no_os_secret_leak(tmp_path, monkeypatch):
    _, _, _, env, _ = _invoke(tmp_path, monkeypatch)
    assert env["CAIRN_CANARY"] == "canary-value"
    assert "OS_ONLY_SECRET" not in env  # os.environ was NOT merged into the child


def test_runs_in_the_run_dir(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    inv = make_inv(tmp_path, scratch=scratch, model="anthropic/claude-opus-4.8")
    HermesExecutor(CFG).invoke(inv)
    assert (scratch / "cwd.txt").read_text() == str(inv.cwd)


def test_step_parsed_and_exit_code(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    inv = make_inv(tmp_path, scratch=scratch, env=fake_env(scratch, CAIRN_TEST_EXIT="4"))
    result = HermesExecutor(CFG).invoke(inv)
    assert result.exit_code == 4
    assert result.step == {"status": "done", "summary": "fake ok", "artifacts": []}


# --------------------------------------------------------------------------- #
# capabilities + model resolution
# --------------------------------------------------------------------------- #


def test_capabilities():
    caps = HermesExecutor(CFG).capabilities
    assert caps == Capabilities(
        blocking_hooks=None, output_schema=False, session_capture=None,
        installs_hooks=False, sandbox="fs",
    )


def test_resolve_model_fixed_and_passthrough_effort():
    ex = HermesExecutor(CFG)
    assert ex.resolve_model("reasoning", "low") == ("anthropic/claude-opus-4.8", "high")  # tier fixes effort
    assert ex.resolve_model("cheap", "medium") == ("anthropic/claude-haiku-4.5", "medium")  # passthrough


def test_resolve_model_unknown_tier_raises():
    with pytest.raises(ConfigError):
        HermesExecutor(CFG).resolve_model("no-such-tier", "high")


def test_render_workspace_writes_nothing(tmp_path):
    # _workspace_file is None (doctrine rides the prompt; --ignore-rules would suppress AGENTS.md).
    from types import SimpleNamespace

    doctrine = tmp_path / "DOCTRINE.md"
    doctrine.write_text("hermes doctrine", encoding="utf-8")
    HermesExecutor(CFG).render_workspace(SimpleNamespace(root=tmp_path, doctrine=doctrine))
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "CLAUDE.md").exists()


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def test_doctor_healthy_with_fake_version(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    # Hermetic: stub the sandbox primitive present (fs posture) so this asserts doctor's logic on
    # a healthy machine, not whether the host ships sandbox-exec/bwrap (covered in test_sandbox.py).
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")  # satisfy the API-key auth advisory
    assert HermesExecutor(CFG).doctor() == []


def test_doctor_reports_missing_binary(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    findings = HermesExecutor(CFG).doctor()
    assert any(f.level == "error" and f.fix for f in findings)


def test_doctor_warns_on_pin_mismatch(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setenv("CAIRN_TEST_VERSION", "hermes 0.99.0 (fake)")
    findings = HermesExecutor(CFG).doctor()
    assert any(f.level == "warning" and "0.18" in f.message for f in findings)


def test_doctor_warns_when_api_key_absent(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    findings = HermesExecutor(CFG).doctor()
    assert any(f.level == "warning" and "API-key" in f.message for f in findings)
    assert not any(f.level == "error" for f in findings)  # advisory, never a gate


def test_doctor_no_api_key_warning_when_present(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake")  # either known var satisfies it
    findings = HermesExecutor(CFG).doctor()
    assert not any("API-key" in f.message for f in findings)


def test_doctor_no_auth_warn_stacked_on_missing_binary(monkeypatch):
    # The auth advisory is suppressed once the binary/version probe already hard-errored.
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    findings = HermesExecutor(CFG).doctor()
    assert not any("API-key" in f.message for f in findings)


# --------------------------------------------------------------------------- #
# W5b — doctor flag-drift checks (sub-change A.1)
# --------------------------------------------------------------------------- #


def test_doctor_warns_when_emitted_flag_missing_from_help(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setenv("CAIRN_TEST_HELP_OMIT", "--usage-file")
    findings = HermesExecutor(CFG).doctor()
    assert any(
        f.level == "warning" and "--usage-file" in f.message and "not advertised" in f.message
        for f in findings
    )


def test_doctor_no_flag_warning_on_healthy_help(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    findings = HermesExecutor(CFG).doctor()
    assert not any("not advertised" in f.message for f in findings)


def test_doctor_warns_when_help_fetch_fails(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setenv("CAIRN_TEST_HELP_FAIL", "1")
    findings = HermesExecutor(CFG).doctor()
    assert any(f.level == "warning" and "could not run" in f.message for f in findings)
    assert sum("not advertised" in f.message for f in findings) == 0


def test_doctor_never_hard_fails_on_flag_drift(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setenv("CAIRN_TEST_HELP_OMIT", "-z,-m,--yolo")
    findings = HermesExecutor(CFG).doctor()
    assert findings  # something warned
    assert not any(f.level == "error" for f in findings)
