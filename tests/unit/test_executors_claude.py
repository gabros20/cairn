"""ClaudeExecutor — prompt as an argv arg, --effort omitted when None, blocking hooks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cairn.executors.claude import ClaudeExecutor
from cairn.kernel.config import ExecutorConfig, TierSpec
from cairn.kernel.errors import ConfigError
from cairn.kernel.types import Capabilities

from test_executors_base import make_inv, read_scratch, use_fakebin

CFG = ExecutorConfig(
    name="claude",
    tiers={
        "reasoning": TierSpec(model="opus", effort="high"),
        "cheap": TierSpec(model="haiku"),  # effort omitted → accept the agent's effort
    },
)


def _invoke(tmp_path, monkeypatch, **kw):
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    inv = make_inv(tmp_path, scratch=scratch, env=None, **kw)
    result = ClaudeExecutor(CFG).invoke(inv)
    argv, env, stdin = read_scratch(scratch)
    return result, inv, argv, env, stdin


def test_argv_shape_prompt_as_arg(tmp_path, monkeypatch):
    _, inv, argv, _, stdin = _invoke(tmp_path, monkeypatch, prompt="hello prompt", model="opus", effort="high")
    assert argv[0].split("/")[-1] == "claude"  # argv[0] is the PATH-resolved binary
    assert argv[1:] == [
        "-p", "hello prompt",
        "--model", "opus", "--effort", "high",
        "--output-format", "text",
        "--permission-mode", "bypassPermissions",
    ]
    assert stdin == ""  # claude gets the prompt as an arg, not on stdin


def test_headless_permission_mode_is_bypass(tmp_path, monkeypatch):
    # Without a permission mode a headless `claude -p` refuses every tool use ("I need your
    # permission…") and never writes its artifact. cairn's own guards (blocking PreToolUse
    # hooks) are the enforcement layer, so the executor runs claude fully non-interactive.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch)
    assert argv[-2:] == ["--permission-mode", "bypassPermissions"]


def test_effort_pair_omitted_when_none(tmp_path, monkeypatch):
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    assert "--effort" not in argv


def test_env_passed_exactly(tmp_path, monkeypatch):
    _, _, _, env, _ = _invoke(tmp_path, monkeypatch)
    assert env["CAIRN_CANARY"] == "canary-value"
    assert "OS_ONLY_SECRET" not in env  # os.environ was not inherited


def test_cwd_is_the_run_dir(tmp_path, monkeypatch):
    _, inv, _, _, _ = _invoke(tmp_path, monkeypatch)
    assert (tmp_path / "scratch" / "cwd.txt").read_text() == str(inv.cwd)


def test_step_parsed_and_log_teed(tmp_path, monkeypatch):
    result, inv, _, _, _ = _invoke(tmp_path, monkeypatch)
    assert result.step == {"status": "done", "summary": "fake ok", "artifacts": []}
    assert "chatty claude preamble" in inv.log_path.read_text()


def test_exit_code_propagated(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    from test_executors_base import fake_env

    inv = make_inv(tmp_path, scratch=scratch, env=fake_env(scratch, CAIRN_TEST_EXIT="4"))
    assert ClaudeExecutor(CFG).invoke(inv).exit_code == 4


def test_capabilities():
    caps = ClaudeExecutor(CFG).capabilities
    assert caps == Capabilities(
        blocking_hooks=True, output_schema=False, session_capture="~/.claude/projects/**"
    )


def test_resolve_model_fixed_and_passthrough_effort():
    ex = ClaudeExecutor(CFG)
    assert ex.resolve_model("reasoning", "low") == ("opus", "high")  # tier fixes effort
    assert ex.resolve_model("cheap", "low") == ("haiku", "low")  # tier accepts agent effort


def test_resolve_model_unknown_tier_raises():
    with pytest.raises(ConfigError):
        ClaudeExecutor(CFG).resolve_model("no-such-tier", "high")


def test_doctor_healthy_with_fake_version(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    assert ClaudeExecutor(CFG).doctor() == []


def test_doctor_reports_missing_binary(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    findings = ClaudeExecutor(CFG).doctor()
    assert any(f.level == "error" and f.fix for f in findings)


def test_render_workspace_writes_and_is_idempotent(tmp_path):
    doctrine = tmp_path / "DOCTRINE.md"
    doctrine.write_text("BE EXCELLENT.", encoding="utf-8")
    ws = SimpleNamespace(root=tmp_path, doctrine=doctrine)
    ex = ClaudeExecutor(CFG)

    ex.render_workspace(ws)
    target = tmp_path / "CLAUDE.md"
    assert target.exists()
    assert "BE EXCELLENT." in target.read_text()
    mtime = target.stat().st_mtime_ns

    ex.render_workspace(ws)  # no change → must not rewrite
    assert target.stat().st_mtime_ns == mtime


def test_render_workspace_rewrites_on_content_change(tmp_path):
    doctrine = tmp_path / "DOCTRINE.md"
    doctrine.write_text("v1", encoding="utf-8")
    ws = SimpleNamespace(root=tmp_path, doctrine=doctrine)
    ex = ClaudeExecutor(CFG)
    ex.render_workspace(ws)
    doctrine.write_text("v2 changed", encoding="utf-8")
    ex.render_workspace(ws)
    assert "v2 changed" in (tmp_path / "CLAUDE.md").read_text()


def test_install_guards_is_a_noop(tmp_path):
    assert ClaudeExecutor(CFG).install_guards([], SimpleNamespace(root=tmp_path)) is None
