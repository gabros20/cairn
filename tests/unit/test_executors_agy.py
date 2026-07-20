"""AgyExecutor — prompt on argv, per-run relocated HOME config isolation, effort
accepted-and-dropped, sandbox posture fs. Local-only executor (no CI auth path).

agy is not installed in this env — every argv/format fact is [DOC]/[UNC] from
.orchestrate/xcli-feasibility-agy.md and its CONTROLLER ARBITRATION section; the fake CLI stands
in for the real one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairn.kernel.sandbox import NativeBackend

from cairn.executors.agy import (
    AgyExecutor,
    _parse_agy_models,
    render_settings_json,
)
from cairn.kernel.config import ExecutorConfig, TierSpec
from cairn.kernel.errors import ConfigError
from cairn.kernel.types import Capabilities

from test_executors_base import fake_env, make_inv, read_scratch, use_fakebin

CFG = ExecutorConfig(
    name="agy",
    pin_version="1.1.4",
    tiers={
        "reasoning": TierSpec(model="Gemini 3.1 Pro", effort="high"),  # tier fixes effort
        "cheap": TierSpec(model="Gemini 3.5 Flash"),  # agent effort passes through
    },
)


def _invoke(tmp_path, monkeypatch, **kw):
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    kw.setdefault("model", "Gemini 3.1 Pro")
    inv = make_inv(tmp_path, scratch=scratch, **kw)
    result = AgyExecutor(CFG).invoke(inv)
    argv, env, stdin = read_scratch(scratch)
    return result, inv, argv, env, stdin


def _agy_home(inv) -> Path:
    """The per-invocation HOME the executor derives (keyed on log_path.stem — H1)."""
    return inv.cwd / ".cairn" / f"agy-home.{inv.log_path.stem}"


def _agy_settings(inv) -> dict:
    """Parse the settings.json the executor pre-seeded under the relocated HOME."""
    path = _agy_home(inv) / ".gemini" / "antigravity-cli" / "settings.json"
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# argv shape + prompt delivery
# --------------------------------------------------------------------------- #


def test_argv_shape_and_prompt_on_argv(tmp_path, monkeypatch):
    _, inv, argv, _, stdin = _invoke(
        tmp_path, monkeypatch, prompt="the agy prompt", effort="high", timeout_s=30
    )
    assert argv[0].split("/")[-1] == "agy"  # argv[0] is the PATH-resolved binary
    # Exact pin of the argv so any drift fails loudly (codex/grok standard).
    assert argv[1:] == [
        "-p", "the agy prompt",
        "--cwd", str(inv.cwd),
        "--model", "Gemini 3.1 Pro",
        "--dangerously-skip-permissions",
        "--print-timeout", "25s",  # inv.timeout_s (30) minus the 5s fail-fast margin (A2-L1)
    ]
    assert stdin == ""  # agy gets the prompt on argv, not stdin (stdin_text=None); §2 CHANGELOG 1.1.1


def test_prompt_is_on_argv_not_stdin(tmp_path, monkeypatch):
    # Explicit inverse of the claude/codex stdin contract: since 1.1.1 agy reads stdin ONLY when
    # `-p` has no inline arg, so the envelope IS on the ps surface here (report §2).
    _, _, argv, _, stdin = _invoke(tmp_path, monkeypatch, prompt="ENVELOPE-BODY")
    assert "ENVELOPE-BODY" in argv
    assert stdin == ""


def test_print_timeout_is_invocation_timeout_minus_margin(tmp_path, monkeypatch):
    # agy takes a duration STRING; cairn emits `<int(timeout_s) - 5>s` (quality-review A2-L1):
    # run_process's clock starts first, so an EQUAL bound means cairn's group-SIGKILL wins the
    # tie and agy's graceful fail-fast (1.1.2, actionable stderr + non-zero exit) never runs.
    # The 5s margin lets agy fail first; run_process stays the hard ceiling.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, timeout_s=120)
    i = argv.index("--print-timeout")
    assert argv[i + 1] == "115s"


def test_print_timeout_clamps_to_one_second_floor(tmp_path, monkeypatch):
    # max(1, …): a budget at or under the margin still yields a duration-grammar-valid "1s",
    # never "0s"/negative.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, timeout_s=3)
    i = argv.index("--print-timeout")
    assert argv[i + 1] == "1s"


def test_sandbox_flag_not_emitted(tmp_path, monkeypatch):
    # Arbitration: `--sandbox` is DROPPED from argv (nested-seatbelt risk under cairn's own FS
    # wrap). `--dangerously-skip-permissions` IS emitted; cairn's fs posture is the containment.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch)
    assert "--sandbox" not in argv
    assert "--dangerously-skip-permissions" in argv


def test_model_pinned_on_argv(tmp_path, monkeypatch):
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, model="Gemini 3.5 Flash")
    i = argv.index("--model")
    assert argv[i + 1] == "Gemini 3.5 Flash"


def test_step_sentinel_parses(tmp_path, monkeypatch):
    result, _, _, _, _ = _invoke(tmp_path, monkeypatch)
    assert result.step == {"status": "done", "summary": "fake ok", "artifacts": []}


# --------------------------------------------------------------------------- #
# effort: accepted-and-dropped (no flag; suffixed-slug is the operator's lever)
# --------------------------------------------------------------------------- #


def test_effort_is_dropped_never_emitted(tmp_path, monkeypatch):
    # agy has no effort flag (report §3 [UNC] suffixed-slug mechanism only); inv.effort is
    # accepted-and-dropped. No effort token ever reaches argv.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort="high")
    assert "--effort" not in argv
    assert "--reasoning-effort" not in argv
    assert "high" not in argv  # the effort VALUE never leaks onto argv either


def test_argv_identical_regardless_of_effort(tmp_path, monkeypatch):
    # Proof that effort is fully dropped: the argv for effort=None, effort="low", and effort="max"
    # are byte-identical (only the model/cwd/timeout — never effort — shape it).
    def argv_for(effort):
        use_fakebin(monkeypatch)
        scratch = tmp_path / f"scratch-{effort}"
        inv = make_inv(
            tmp_path, scratch=scratch, model="Gemini 3.1 Pro", effort=effort,
            log_path=tmp_path / "logs" / f"step-{effort}.log",
        )
        AgyExecutor(CFG).invoke(inv)
        argv, _, _ = read_scratch(scratch)
        return argv[1:]

    base = argv_for(None)
    assert argv_for("low") == base
    assert argv_for("max") == base


# --------------------------------------------------------------------------- #
# per-run relocated HOME config isolation + pre-seeded settings.json
# --------------------------------------------------------------------------- #


def test_home_points_under_run_dir(tmp_path, monkeypatch):
    _, inv, _, env, _ = _invoke(tmp_path, monkeypatch)
    expected = _agy_home(inv)
    assert env["HOME"] == str(expected)
    assert (expected / ".gemini" / "antigravity-cli" / "settings.json").is_file()


def test_home_override_never_touches_user_or_logname(tmp_path, monkeypatch):
    # CAUTION comment in _extra_env: only HOME is relocated; USER/LOGNAME stay whatever the sealed
    # baseline passed through (keyring auth is UID/session-scoped, not HOME-scoped — smoke item #1).
    ex = AgyExecutor(CFG)
    scratch = tmp_path / "scratch"
    inv = make_inv(tmp_path, scratch=scratch, model="Gemini 3.1 Pro")
    extra = ex._extra_env(inv)
    assert set(extra) == {"HOME", "AGY_CLI_DISABLE_AUTO_UPDATE"}
    assert "USER" not in extra and "LOGNAME" not in extra


def test_settings_json_content(tmp_path, monkeypatch):
    # Pre-seeded settings we KNOW the paths for: telemetry off, terminal sandbox off explicitly
    # (report §5/§11). The trust store is NOT seeded (its format is [UNC]).
    _, inv, _, _, _ = _invoke(tmp_path, monkeypatch)
    settings = _agy_settings(inv)
    assert settings == {"enableTelemetry": False, "enableTerminalSandbox": False}


def test_render_settings_json_is_valid_json():
    # Direct unit test of the pure emitter: round-trips through json.loads with the exact keys.
    parsed = json.loads(render_settings_json())
    assert parsed == {"enableTelemetry": False, "enableTerminalSandbox": False}
    assert parsed["enableTelemetry"] is False  # a real bool, not the string "false"


def test_no_secret_written_to_settings(tmp_path, monkeypatch):
    # brief rule 4: no secret on disk. agy has no key-in-config path (auth is OS-keyring), so the
    # settings file must carry no credential-shaped value even when one is present in inv.env.
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    env = fake_env(scratch, GEMINI_API_KEY="sk-super-secret-VALUE")
    inv = make_inv(tmp_path, scratch=scratch, model="Gemini 3.1 Pro", env=env)
    AgyExecutor(CFG).invoke(inv)
    settings_text = (
        _agy_home(inv) / ".gemini" / "antigravity-cli" / "settings.json"
    ).read_text()
    assert "sk-super-secret-VALUE" not in settings_text


def test_ci_hygiene_auto_update_off(tmp_path, monkeypatch):
    # report §11: the background self-updater holds an advisory lock that can block concurrent
    # invocations — disabled for headless determinism.
    _, _, _, env, _ = _invoke(tmp_path, monkeypatch)
    assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"


def test_parallel_steps_get_isolated_homes(tmp_path, monkeypatch):
    # H1 (xcli-review-quality.md): a ParallelNode runs its children concurrently on the SAME
    # executor instance, and walk.py sets inv.cwd = self.run_dir for every step — so two parallel
    # agy steps share one cwd. Keying HOME on inv.cwd alone would let them share one settings.json
    # and one last_conversations.json (cwd→conversation reuse, §7). Keying on log_path.stem gives
    # each its own home. This test fails on the inv.cwd-only design (identical HOME paths).
    use_fakebin(monkeypatch)
    scratch_a = tmp_path / "scratch-a"
    scratch_b = tmp_path / "scratch-b"
    inv_a = make_inv(
        tmp_path, scratch=scratch_a, model="Gemini 3.1 Pro",
        log_path=tmp_path / "logs" / "stepA.log",
    )
    inv_b = make_inv(
        tmp_path, scratch=scratch_b, model="Gemini 3.5 Flash",
        log_path=tmp_path / "logs" / "stepB.log",
    )
    assert inv_a.cwd == inv_b.cwd  # both steps of one run share the run dir (walk.py)

    ex = AgyExecutor(CFG)
    ex.invoke(inv_a)
    ex.invoke(inv_b)

    _, env_a, _ = read_scratch(scratch_a)
    _, env_b, _ = read_scratch(scratch_b)
    assert env_a["HOME"] != env_b["HOME"]  # distinct homes, no shared settings/conversation race
    # Each home has its own seeded settings.json.
    for home in (env_a["HOME"], env_b["HOME"]):
        assert (Path(home) / ".gemini" / "antigravity-cli" / "settings.json").is_file()


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
    inv = make_inv(
        tmp_path, scratch=scratch, model="Gemini 3.1 Pro",
        env=fake_env(scratch, CAIRN_TEST_EXIT="4"),
    )
    result = AgyExecutor(CFG).invoke(inv)
    assert result.exit_code == 4
    assert result.step == {"status": "done", "summary": "fake ok", "artifacts": []}


# --------------------------------------------------------------------------- #
# model-roster parser (format is [UNC] — defensive)
# --------------------------------------------------------------------------- #


def test_parse_agy_models_handles_slugs_with_spaces():
    text = (
        "Available models:\n"
        "  * Gemini 3.5 Flash (default)\n"
        "  - Gemini 3.1 Pro\n"
        "  - Claude Sonnet 4.6 (thinking)\n"
    )
    assert _parse_agy_models(text) == {
        "Gemini 3.5 Flash", "Gemini 3.1 Pro", "Claude Sonnet 4.6 (thinking)"
    }


def test_parse_agy_models_unparseable_is_empty():
    # No bullet lines ⇒ empty set (the caller turns this into a warning, never a crash).
    assert _parse_agy_models("some banner\nno models here\n") == set()
    assert _parse_agy_models("") == set()


# --------------------------------------------------------------------------- #
# capabilities + model resolution
# --------------------------------------------------------------------------- #


def test_capabilities():
    caps = AgyExecutor(CFG).capabilities
    # blocking_hooks None (headless firing UNVERIFIED — doctor probes later); output_schema False
    # (plain text only, no --json ever shipped); session_capture None (structural, via throwaway
    # HOME); installs_hooks False; sandbox fs (OS FS-wrap, agy's own --sandbox dropped).
    assert caps == Capabilities(
        blocking_hooks=None, output_schema=False, session_capture=None,
        installs_hooks=False, sandbox="fs",
    )


def test_resolve_model_fixed_and_passthrough_effort():
    ex = AgyExecutor(CFG)
    # Even though effort is dropped at argv time, resolve_model still returns it (the walker owns
    # that contract); tier-fixed effort wins, otherwise the agent's effort flows through.
    assert ex.resolve_model("reasoning", "low") == ("Gemini 3.1 Pro", "high")  # tier fixes effort
    assert ex.resolve_model("cheap", "medium") == ("Gemini 3.5 Flash", "medium")  # passthrough


def test_resolve_model_unknown_tier_raises():
    with pytest.raises(ConfigError):
        AgyExecutor(CFG).resolve_model("no-such-tier", "high")


def test_render_workspace_writes_agents_md(tmp_path):
    from types import SimpleNamespace

    doctrine = tmp_path / "DOCTRINE.md"
    doctrine.write_text("agy doctrine", encoding="utf-8")
    AgyExecutor(CFG).render_workspace(SimpleNamespace(root=tmp_path, doctrine=doctrine))
    assert "agy doctrine" in (tmp_path / "AGENTS.md").read_text()
    assert not (tmp_path / "CLAUDE.md").exists()


# --------------------------------------------------------------------------- #
# doctor — version/pin + auth (via `agy models`) + flag/model drift
# --------------------------------------------------------------------------- #


def test_doctor_healthy_with_fake_version(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)  # fake prints "agy 1.1.4 (fake)" → satisfies pin 1.1.4
    # Hermetic: stub the sandbox primitive present so this asserts doctor's logic on a healthy
    # machine, not whether the host has sandbox-exec/bwrap (the fs-posture WARN branch is covered
    # hermetically in test_sandbox.py).
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    assert AgyExecutor(CFG).doctor() == []


def test_doctor_reports_missing_binary(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    findings = AgyExecutor(CFG).doctor()
    assert any(f.level == "error" and f.fix for f in findings)


def test_doctor_warns_on_pin_mismatch(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("CAIRN_TEST_VERSION", "agy 0.9.0 (fake)")
    findings = AgyExecutor(CFG).doctor()
    assert any(f.level == "warning" and "1.1.4" in f.message for f in findings)


def test_doctor_errors_actionably_when_unauthenticated(tmp_path, monkeypatch):
    # `agy models` failing is the local unauthenticated signal (report §1). doctor must ERROR
    # actionably (arbitration), naming the interactive-only sign-in and the absent CI auth path.
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("CAIRN_TEST_MODELS_FAIL", "1")
    findings = AgyExecutor(CFG).doctor()
    auth_errs = [
        f for f in findings
        if f.level == "error" and "interactive-only" in f.message and "GH #78" in f.message
    ]
    assert auth_errs
    assert auth_errs[0].fix  # actionable, copy-pasteable remedy


def test_doctor_warns_on_unknown_model_slug(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("CAIRN_TEST_MODELS", "Gemini 3.5 Flash,Gemini 3.1 Pro")
    cfg = ExecutorConfig(
        name="agy", pin_version="1.1.4",
        tiers={"cheap": TierSpec(model="Gemini 9.9 Nonexistent")},
    )
    findings = AgyExecutor(cfg).doctor()
    assert any(
        f.level == "warning" and "Gemini 9.9 Nonexistent" in f.message for f in findings
    )


def test_doctor_no_model_warning_when_all_tiers_known(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)  # default roster covers CFG's tier models
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    findings = AgyExecutor(CFG).doctor()
    assert not any("not in `agy models`" in f.message for f in findings)


def test_doctor_warns_when_models_unparseable(tmp_path, monkeypatch):
    # Empty roster (CAIRN_TEST_MODELS="") ⇒ the header prints with no bullets. The [UNC] format
    # degrades to one warning, not a crash and not a per-tier false positive.
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("CAIRN_TEST_MODELS", "")
    findings = AgyExecutor(CFG).doctor()
    assert any(
        f.level == "warning" and "no parseable model list" in f.message for f in findings
    )
    assert not any(f.level == "error" for f in findings)


def test_doctor_warns_when_emitted_flag_missing_from_help(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("CAIRN_TEST_HELP_OMIT", "--print-timeout")
    findings = AgyExecutor(CFG).doctor()
    assert any(
        f.level == "warning" and "--print-timeout" in f.message and "not advertised" in f.message
        for f in findings
    )


def test_doctor_no_flag_warning_on_healthy_help(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    findings = AgyExecutor(CFG).doctor()
    assert not any("not advertised" in f.message for f in findings)


def test_doctor_warns_when_help_fetch_fails(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("CAIRN_TEST_HELP_FAIL", "1")
    findings = AgyExecutor(CFG).doctor()
    assert any(f.level == "warning" and "could not run" in f.message for f in findings)
    assert sum("not advertised" in f.message for f in findings) == 0


def test_doctor_never_hard_fails_on_flag_drift(tmp_path, monkeypatch):
    # Flag drift alone is never an error (auth is fine here — models still succeed).
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("CAIRN_TEST_HELP_OMIT", "-p,--model,--print-timeout")
    findings = AgyExecutor(CFG).doctor()
    assert findings  # something warned
    assert not any(f.level == "error" for f in findings)
