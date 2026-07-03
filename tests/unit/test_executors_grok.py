"""GrokExecutor — prompt via stdin, effort always None (BYOK aliases bake it), no workspace file."""

from __future__ import annotations

from types import SimpleNamespace

from cairn.executors.grok import GrokExecutor
from cairn.kernel.config import ExecutorConfig, TierSpec
from cairn.kernel.types import Capabilities

from test_executors_base import make_inv, read_scratch, use_fakebin

CFG = ExecutorConfig(
    name="grok",
    tiers={
        "reasoning": TierSpec(model="grok-4.3-high"),  # alias bakes the effort
        "weird": TierSpec(model="grok-x", effort="high"),  # even a set effort must be dropped
    },
)


def _invoke(tmp_path, monkeypatch, **kw):
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    inv = make_inv(tmp_path, scratch=scratch, model="grok-4.3-high", effort=None, **kw)
    result = GrokExecutor(CFG).invoke(inv)
    argv, env, stdin = read_scratch(scratch)
    return result, inv, argv, env, stdin


def test_argv_shape_and_prompt_on_stdin(tmp_path, monkeypatch):
    _, inv, argv, _, stdin = _invoke(tmp_path, monkeypatch, prompt="grok prompt body")
    assert argv[0].split("/")[-1] == "grok"
    assert argv[1:] == [
        "-p", "--cwd", str(inv.cwd), "-m", "grok-4.3-high",
        "--output-format", "text", "--permission-mode", "dontAsk",
        "--no-alt-screen", "--no-auto-update",
    ]
    assert stdin == "grok prompt body"
    assert "--effort" not in argv  # Grok has no effort flag


def test_env_passed_exactly_and_step_parsed(tmp_path, monkeypatch):
    result, _, _, env, _ = _invoke(tmp_path, monkeypatch)
    assert env["CAIRN_CANARY"] == "canary-value"
    assert "OS_ONLY_SECRET" not in env
    assert result.step == {"status": "done", "summary": "fake ok", "artifacts": []}


def test_resolve_model_always_drops_effort():
    ex = GrokExecutor(CFG)
    assert ex.resolve_model("reasoning", "high") == ("grok-4.3-high", None)
    assert ex.resolve_model("weird", "high") == ("grok-x", None)  # baked, not passed through


def test_capabilities():
    caps = GrokExecutor(CFG).capabilities
    assert caps == Capabilities(
        blocking_hooks=True, output_schema=False, session_capture=None
    )


def test_render_workspace_is_a_noop(tmp_path):
    doctrine = tmp_path / "DOCTRINE.md"
    doctrine.write_text("x", encoding="utf-8")
    GrokExecutor(CFG).render_workspace(SimpleNamespace(root=tmp_path, doctrine=doctrine))
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / "AGENTS.md").exists()  # grok reads both; writes neither


def test_doctor_healthy_with_fake_version(monkeypatch):
    use_fakebin(monkeypatch)
    assert GrokExecutor(CFG).doctor() == []
