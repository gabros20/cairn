"""CodexExecutor — prompt via stdin, sandbox/approval flags, effort as -c override, pin doctor."""

from __future__ import annotations

from cairn.executors.codex import CodexExecutor
from cairn.kernel.config import ExecutorConfig, TierSpec
from cairn.kernel.types import Capabilities

from test_executors_base import fake_env, make_inv, read_scratch, use_fakebin

CFG = ExecutorConfig(
    name="codex",
    pin_version="0.138",
    tiers={
        "reasoning": TierSpec(model="gpt-5.5", effort="high"),
        "cheap": TierSpec(model="gpt-5.4-mini"),
    },
)


def _invoke(tmp_path, monkeypatch, **kw):
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    inv = make_inv(tmp_path, scratch=scratch, model="gpt-5.5", **kw)
    result = CodexExecutor(CFG).invoke(inv)
    argv, env, stdin = read_scratch(scratch)
    return result, inv, argv, env, stdin


def test_argv_shape_and_prompt_on_stdin(tmp_path, monkeypatch):
    _, inv, argv, _, stdin = _invoke(tmp_path, monkeypatch, prompt="the codex prompt", effort="high")
    assert argv[0].split("/")[-1] == "codex"
    assert argv[1:] == [
        "exec", "-C", str(inv.cwd), "-m", "gpt-5.5",
        "--sandbox", "workspace-write", "--skip-git-repo-check",
        "--ignore-user-config", "--ignore-rules",
        "-c", "model_reasoning_effort=high",
    ]
    assert stdin == "the codex prompt"  # prompt is delivered on stdin, not argv


def test_config_isolation_flags_present(tmp_path, monkeypatch):
    # W4 (codex-F6): seal the process from ambient user config for deterministic runs.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv


def test_no_approval_flag(tmp_path, monkeypatch):
    # codex-cli 0.142.5 removed `-a/--ask-for-approval` from `codex exec` (approval is
    # hardwired to `never` in exec mode); passing `-a never` is an argv error. Live-verified.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    assert "-a" not in argv and "--ask-for-approval" not in argv


def test_skip_git_repo_check_present(tmp_path, monkeypatch):
    # Without it, `codex exec` refuses to run in a cwd that is neither a git repo nor a
    # trusted directory ("Not inside a trusted directory and --skip-git-repo-check was not
    # specified.") — cairn run dirs are arbitrary. Live-verified on 0.142.5.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    assert "--skip-git-repo-check" in argv


def test_effort_override_omitted_when_none(tmp_path, monkeypatch):
    _, inv, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    # Exact pin of the no-effort argv so drift in this branch fails loudly too.
    assert argv[1:] == [
        "exec", "-C", str(inv.cwd), "-m", "gpt-5.5",
        "--sandbox", "workspace-write", "--skip-git-repo-check",
        "--ignore-user-config", "--ignore-rules",
    ]
    assert not any(a.startswith("model_reasoning_effort=") for a in argv)
    assert "-c" not in argv


def test_env_passed_exactly(tmp_path, monkeypatch):
    _, _, _, env, _ = _invoke(tmp_path, monkeypatch)
    assert env["CAIRN_CANARY"] == "canary-value"
    assert "OS_ONLY_SECRET" not in env


def test_step_parsed_and_exit_code(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    inv = make_inv(tmp_path, scratch=scratch, env=fake_env(scratch, CAIRN_TEST_EXIT="4"))
    result = CodexExecutor(CFG).invoke(inv)
    assert result.exit_code == 4
    assert result.step == {"status": "done", "summary": "fake ok", "artifacts": []}


def test_capabilities():
    caps = CodexExecutor(CFG).capabilities
    # blocking_hooks UNVERIFIED headless → None (doctor probes later); output_schema native.
    assert caps == Capabilities(
        blocking_hooks=None, output_schema=True, session_capture="~/.codex/sessions/**",
        installs_hooks=False,
    )


def test_resolve_model():
    ex = CodexExecutor(CFG)
    assert ex.resolve_model("reasoning", "low") == ("gpt-5.5", "high")
    assert ex.resolve_model("cheap", "medium") == ("gpt-5.4-mini", "medium")


def test_render_workspace_writes_agents_md(tmp_path):
    from types import SimpleNamespace

    doctrine = tmp_path / "DOCTRINE.md"
    doctrine.write_text("codex doctrine", encoding="utf-8")
    CodexExecutor(CFG).render_workspace(SimpleNamespace(root=tmp_path, doctrine=doctrine))
    assert "codex doctrine" in (tmp_path / "AGENTS.md").read_text()
    assert not (tmp_path / "CLAUDE.md").exists()


def test_doctor_healthy_when_version_matches_pin(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)  # fake prints "codex 0.138.0" → satisfies pin 0.138
    assert CodexExecutor(CFG).doctor() == []


def test_doctor_warns_on_pin_mismatch(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setenv("CAIRN_TEST_VERSION", "codex 0.99.0 (fake)")
    findings = CodexExecutor(CFG).doctor()
    assert any(f.level == "warning" and "0.138" in f.message for f in findings)
