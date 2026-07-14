"""GrokExecutor — prompt via --prompt-file, native --effort, bypassPermissions, no workspace file.

Every argv fact here was live-verified against grok 0.2.82 (6d0b07d2de0f); see the comments
in cairn/executors/grok.py for the observed failures behind each choice.
"""

from __future__ import annotations

from types import SimpleNamespace

from cairn.executors.grok import GrokExecutor
from cairn.kernel.config import ExecutorConfig, TierSpec
from cairn.kernel.types import Capabilities

from test_executors_base import make_inv, read_scratch, use_fakebin

CFG = ExecutorConfig(
    name="grok",
    tiers={
        "reasoning": TierSpec(model="grok-build", effort="high"),  # tier fixes effort
        "cheap": TierSpec(model="grok-composer-2.5-fast"),  # agent effort passes through
    },
)


def _invoke(tmp_path, monkeypatch, **kw):
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    inv = make_inv(tmp_path, scratch=scratch, model="grok-build", **kw)
    result = GrokExecutor(CFG).invoke(inv)
    argv, env, stdin = read_scratch(scratch)
    return result, inv, argv, env, stdin


def test_argv_shape_prompt_via_prompt_file_with_effort(tmp_path, monkeypatch):
    _, inv, argv, _, stdin = _invoke(tmp_path, monkeypatch, prompt="grok prompt body", effort="low")
    assert argv[0].split("/")[-1] == "grok"
    # Exact pin of the effort-branch argv so any drift fails loudly (codex standard).
    assert argv[1:] == [
        "--prompt-file", str(inv.prompt_file),
        "--cwd", str(inv.cwd),
        "-m", "grok-build",
        "--output-format", "plain",
        "--permission-mode", "bypassPermissions",
        "--no-alt-screen",
        "--no-auto-update",
        "--effort", "low",
    ]
    # grok 0.2.82 headless does NOT read the prompt from stdin (bare `-p` is an argv
    # error: "a value is required for '--single <PROMPT>'"); nothing goes to stdin.
    assert stdin == ""


def test_effort_flag_omitted_when_none(tmp_path, monkeypatch):
    _, inv, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    # Exact pin of the no-effort argv so drift in this branch fails loudly too.
    assert argv[1:] == [
        "--prompt-file", str(inv.prompt_file),
        "--cwd", str(inv.cwd),
        "-m", "grok-build",
        "--output-format", "plain",
        "--permission-mode", "bypassPermissions",
        "--no-alt-screen",
        "--no-auto-update",
    ]
    assert "--effort" not in argv


def test_dead_and_wrong_flags_absent(tmp_path, monkeypatch):
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort="low")
    # `-p/--single` requires the prompt as an ARGUMENT on 0.2.82 and stdin is never read
    # headlessly — the executor delivers the envelope via --prompt-file instead.
    assert "-p" not in argv and "--single" not in argv
    # `--output-format text` is an invalid-value argv error on 0.2.82
    # ("[possible values: plain, json, streaming-json]").
    assert "text" not in argv
    # `--permission-mode dontAsk` silently denies file writes headlessly (exit 0, empty
    # output, NO artifact — reproduced live with a clean GROK_HOME); only
    # bypassPermissions is wired via the flag and actually lets the agent write.
    assert "dontAsk" not in argv
    # `--reasoning-effort` is a different, per-model knob (and both shipped models report
    # supports_reasoning_effort=false); cairn wires the headless `--effort` flag.
    assert "--reasoning-effort" not in argv


def test_env_passed_exactly_and_step_parsed(tmp_path, monkeypatch):
    result, _, _, env, _ = _invoke(tmp_path, monkeypatch, effort=None)
    assert env["CAIRN_CANARY"] == "canary-value"
    assert "OS_ONLY_SECRET" not in env
    assert result.step == {"status": "done", "summary": "fake ok", "artifacts": []}


def test_resolve_model_passes_effort_through():
    # grok 0.2.82 has a native --effort flag whose values cover cairn's EFFORTS, so grok
    # resolves tiers exactly like the other executors: tier-fixed effort wins, otherwise
    # the agent's effort flows through. (The old BYOK "alias bakes effort" override is gone.)
    ex = GrokExecutor(CFG)
    assert ex.resolve_model("reasoning", "low") == ("grok-build", "high")
    assert ex.resolve_model("cheap", "medium") == ("grok-composer-2.5-fast", "medium")


def test_capabilities():
    caps = GrokExecutor(CFG).capabilities
    # blocking_hooks: 0.2.82 ships documented blocking PreToolUse hooks (deny via stdout
    # JSON or exit 2) as a CLI capability — but cairn's own install_guards does NOT wire it
    # (installs_hooks=False), so blocking_hooks correctly stays None (unknown/unasserted; the
    # doctor probe decides) rather than overstating True for a mechanism cairn never installs
    # (grok-F3, W3b). output_schema: native --json-schema exists (not wired; STEP sentinel is
    # the contract).
    assert caps == Capabilities(
        blocking_hooks=None, output_schema=True, session_capture=None, installs_hooks=False,
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
