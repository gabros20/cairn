"""CursorExecutor — argv-trailing prompt, native sandbox flags, CURSOR_CONFIG_DIR isolation,
no effort knob, pin doctor. Binary is `agent` (name == "agent"); "cursor" is the executor's
deliverable/module identity only."""

from __future__ import annotations

import pytest

from cairn.executors.cursor import CursorExecutor
from cairn.kernel.config import ExecutorConfig, TierSpec
from cairn.kernel.errors import ConfigError
from cairn.kernel.types import Capabilities

from test_executors_base import fake_env, make_inv, read_scratch, use_fakebin

CFG = ExecutorConfig(
    name="cursor",
    pin_version="2026.06",
    tiers={
        "reasoning": TierSpec(model="claude-opus-4-8", effort="high"),
        "cheap": TierSpec(model="composer-2.5"),
    },
)


def _invoke(tmp_path, monkeypatch, **kw):
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    inv = make_inv(tmp_path, scratch=scratch, model="claude-opus-4-8", **kw)
    result = CursorExecutor(CFG).invoke(inv)
    argv, env, stdin = read_scratch(scratch)
    return result, inv, argv, env, stdin


def test_argv_shape_and_prompt_as_trailing_arg(tmp_path, monkeypatch):
    _, inv, argv, _, stdin = _invoke(tmp_path, monkeypatch, prompt="the cursor prompt", effort="high")
    assert argv[0].split("/")[-1] == "agent"  # argv[0] is the PATH-resolved binary
    assert argv[1:] == [
        "-p",
        "--workspace", str(inv.cwd),
        "--model", "claude-opus-4-8",
        "--output-format", "text",
        "--sandbox", "enabled",
        "--network", "false",
        "--allow-paths", str(inv.cwd),
        "-f",
        "--trust",
        "the cursor prompt",
    ]
    assert stdin == ""  # prompt delivered as a trailing argv arg, not on stdin


def test_stdin_text_is_none(tmp_path, monkeypatch):
    _, _, _, _, stdin = _invoke(tmp_path, monkeypatch, effort=None)
    assert stdin == ""


def test_effort_never_emitted_no_matter_what(tmp_path, monkeypatch):
    # cursor has no reasoning-effort flag at all (report §3) — inv.effort is never threaded,
    # regardless of value; a documented no-op, never a crash. Token equality, not substring
    # containment — the tmp_path fixture dir is named after this test and itself contains
    # "effort", so a naive substring scan over the whole argv (including --workspace/
    # --allow-paths path values) would false-positive.
    for effort in (None, "low", "high", "max"):
        _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=effort)
        assert "--effort" not in argv
        assert "--reasoning-effort" not in argv


def test_network_defaults_false(tmp_path, monkeypatch):
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    i = argv.index("--network")
    assert argv[i + 1] == "false"


def test_network_true_flows_through(tmp_path, monkeypatch):
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None, network=True)
    i = argv.index("--network")
    assert argv[i + 1] == "true"


def test_force_and_trust_flags_present(tmp_path, monkeypatch):
    # Headless has no answering surface for the default y/n command-approval prompt or the
    # workspace-trust prompt (report §5) — both must be pre-empted unconditionally.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    assert "-f" in argv
    assert "--trust" in argv


def test_sandbox_native_flags_present(tmp_path, monkeypatch):
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    i = argv.index("--sandbox")
    assert argv[i + 1] == "enabled"
    assert "--allow-paths" in argv


def test_cwd_reaches_both_workspace_and_allow_paths(tmp_path, monkeypatch):
    _, inv, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    i = argv.index("--workspace")
    assert argv[i + 1] == str(inv.cwd)
    j = argv.index("--allow-paths")
    assert argv[j + 1] == str(inv.cwd)


def test_env_passed_exactly_and_secret_does_not_leak(tmp_path, monkeypatch):
    _, _, _, env, _ = _invoke(tmp_path, monkeypatch)
    assert env["CAIRN_CANARY"] == "canary-value"
    assert "OS_ONLY_SECRET" not in env


def test_cursor_config_dir_isolation(tmp_path, monkeypatch):
    # W4-style config isolation: CURSOR_CONFIG_DIR points at an ephemeral, cairn-owned
    # directory UNDER the run dir (report §6 — no per-invocation --ignore-user-config exists).
    _, inv, _, env, _ = _invoke(tmp_path, monkeypatch, effort=None)
    expected = inv.cwd / ".cairn" / "cursor-config"
    assert env["CURSOR_CONFIG_DIR"] == str(expected)
    assert expected.is_dir()  # created idempotently by _extra_env


def test_cursor_config_dir_idempotent_across_invocations(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    inv = make_inv(tmp_path, scratch=scratch, model="claude-opus-4-8", effort=None)
    ex = CursorExecutor(CFG)
    ex.invoke(inv)  # first call creates the dir
    ex.invoke(inv)  # second call must not error on an existing dir
    assert (inv.cwd / ".cairn" / "cursor-config").is_dir()


def test_step_parsed_and_exit_code(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    inv = make_inv(tmp_path, scratch=scratch, env=fake_env(scratch, CAIRN_TEST_EXIT="4"))
    result = CursorExecutor(CFG).invoke(inv)
    assert result.exit_code == 4
    assert result.step == {"status": "done", "summary": "fake ok", "artifacts": []}


def test_capabilities():
    caps = CursorExecutor(CFG).capabilities
    assert caps == Capabilities(
        blocking_hooks=None, output_schema=False, session_capture=None,
        installs_hooks=False, sandbox="off",
    )


def test_binary_name_is_agent_not_cursor():
    # report §1: `agent` is the primary entrypoint, `cursor-agent` a compat alias only.
    assert CursorExecutor.name == "agent"


def test_resolve_model():
    ex = CursorExecutor(CFG)
    assert ex.resolve_model("reasoning", "low") == ("claude-opus-4-8", "high")  # tier fixes effort
    assert ex.resolve_model("cheap", "medium") == ("composer-2.5", "medium")  # passthrough


def test_resolve_model_unknown_tier_raises():
    with pytest.raises(ConfigError):
        CursorExecutor(CFG).resolve_model("no-such-tier", "high")


def test_render_workspace_writes_nothing(tmp_path):
    from types import SimpleNamespace

    doctrine = tmp_path / "DOCTRINE.md"
    doctrine.write_text("cursor doctrine", encoding="utf-8")
    CursorExecutor(CFG).render_workspace(SimpleNamespace(root=tmp_path, doctrine=doctrine))
    # cursor natively reads AGENTS.md AND CLAUDE.md (report §9) — cairn writes neither here,
    # same reasoning as grok's _workspace_file = None.
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "CLAUDE.md").exists()


def test_doctor_healthy_when_version_matches_pin(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)  # fake prints "2026.06.02-8c11d9f" → satisfies pin "2026.06"
    assert CursorExecutor(CFG).doctor() == []


def test_doctor_warns_on_pin_mismatch(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setenv("CAIRN_TEST_VERSION", "2025.09.04-fc40cd1")
    findings = CursorExecutor(CFG).doctor()
    assert any(f.level == "warning" and "2026.06" in f.message for f in findings)


def test_doctor_reports_missing_binary(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    findings = CursorExecutor(CFG).doctor()
    assert any(f.level == "error" and f.fix for f in findings)
    assert any("agent" in f.message for f in findings)


def test_doctor_no_model_check_for_cursor(tmp_path, monkeypatch):
    # report §10: no queryable model roster (`/model` is interactive-only) — no
    # _model_findings override, so an unrecognizable-looking model string is silently not
    # checked (never a false WARN), same posture as codex.
    use_fakebin(monkeypatch)
    cfg = ExecutorConfig(name="cursor", tiers={"cheap": TierSpec(model="totally-not-a-real-model")})
    findings = CursorExecutor(cfg).doctor()
    assert findings == []


# --------------------------------------------------------------------------- #
# W5b — doctor drift checks (sub-change A).
# --------------------------------------------------------------------------- #


def test_doctor_warns_when_emitted_flag_missing_from_help(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setenv("CAIRN_TEST_HELP_OMIT", "--trust")
    findings = CursorExecutor(CFG).doctor()
    assert any(
        f.level == "warning" and "--trust" in f.message and "not advertised" in f.message
        for f in findings
    )


def test_doctor_no_flag_warning_on_healthy_help(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    findings = CursorExecutor(CFG).doctor()
    assert not any("not advertised" in f.message for f in findings)


def test_doctor_warns_when_help_fetch_fails(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setenv("CAIRN_TEST_HELP_FAIL", "1")
    findings = CursorExecutor(CFG).doctor()
    assert any(f.level == "warning" and "could not run" in f.message for f in findings)
    assert sum("not advertised" in f.message for f in findings) == 0


def test_doctor_never_hard_fails_on_flag_or_model_drift(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setenv("CAIRN_TEST_HELP_OMIT", "--sandbox,--network,--trust")
    findings = CursorExecutor(CFG).doctor()
    assert findings  # something warned
    assert not any(f.level == "error" for f in findings)


def test_doctor_survives_probe_version_spawn_error(tmp_path, monkeypatch):
    from cairn.kernel.errors import ExecutorSpawnError

    use_fakebin(monkeypatch)

    def _boom(name, timeout_s=15.0):
        raise ExecutorSpawnError(f"{name!r} failed to start", executable=name)

    monkeypatch.setattr("cairn.executors._cli._probe_version", _boom)
    findings = CursorExecutor(CFG).doctor()  # must not raise
    assert len(findings) == 1
    assert findings[0].level == "error"
    assert "failed to run" in findings[0].message
