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
        "--no-memory",
        "--sandbox", "workspace",
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
        "--no-memory",
        "--sandbox", "workspace",
    ]
    assert "--effort" not in argv


def test_config_isolation_flags_present(tmp_path, monkeypatch):
    # W4 (grok-F6/F5): cross-session memory disabled and the workspace sandbox profile applied
    # so identical pipeline runs are deterministic. `workspace` is live-verified against the
    # installed grok CLI (see grok.py's comment) — reads everywhere, writes only CWD + ~/.grok/
    # + temp dirs, matching codex's workspace-write equivalent.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    assert "--no-memory" in argv
    i = argv.index("--sandbox")
    assert argv[i + 1] == "workspace"


def test_effort_max_flows_through(tmp_path, monkeypatch):
    # W4 (claude-F11 Done-when #4): "max" must actually flow through to the emitted argv, not
    # just be accepted by the EFFORTS enum. grok emits it via --effort, which the captured help
    # confirms is an alias for --reasoning-effort (both accept low|medium|high|xhigh|max) — a
    # form the installed grok CLI accepts.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort="max")
    i = argv.index("--effort")
    assert argv[i + 1] == "max"


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
    # (grok-F3, W3b). output_schema=False (W5b sub-change B, grok-F2): native --json-schema
    # exists but is NOT wired — the STEP sentinel is the contract; this previously asserted
    # True, overstating what cairn actually uses.
    assert caps == Capabilities(
        blocking_hooks=None, output_schema=False, session_capture=None, installs_hooks=False,
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


# --------------------------------------------------------------------------- #
# W5b — doctor drift checks (sub-change A).
# --------------------------------------------------------------------------- #


def test_doctor_warns_when_emitted_flag_missing_from_help(monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setenv("CAIRN_TEST_HELP_OMIT", "--sandbox")
    findings = GrokExecutor(CFG).doctor()
    assert any(
        f.level == "warning" and "--sandbox" in f.message and "not advertised" in f.message
        for f in findings
    )


def test_doctor_no_auto_update_never_warns(monkeypatch):
    # --no-auto-update is deliberately excluded from _emitted_flags — grok 0.2.82 hides it
    # from --help while still accepting it, so it must never trigger a flag-drift warning.
    use_fakebin(monkeypatch)
    findings = GrokExecutor(CFG).doctor()
    assert not any("--no-auto-update" in f.message for f in findings)


def test_doctor_warns_on_unknown_model_slug(monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setenv("CAIRN_TEST_MODELS", "grok-build,grok-composer-2.5-fast")
    cfg = ExecutorConfig(
        name="grok", tiers={"cheap": TierSpec(model="grok-this-slug-does-not-exist")}
    )
    findings = GrokExecutor(cfg).doctor()
    assert any(
        f.level == "warning" and "grok-this-slug-does-not-exist" in f.message for f in findings
    )


def test_doctor_no_model_warning_when_all_tiers_known(monkeypatch):
    use_fakebin(monkeypatch)
    findings = GrokExecutor(CFG).doctor()  # CFG models: grok-build, grok-composer-2.5-fast
    assert not any("not in `grok models`" in f.message for f in findings)


def test_doctor_warns_when_models_fetch_fails(monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setenv("CAIRN_TEST_MODELS_FAIL", "1")
    findings = GrokExecutor(CFG).doctor()
    assert any(f.level == "warning" and "could not run" in f.message and "grok models" in f.message for f in findings)


def test_doctor_never_hard_fails_on_drift(monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setenv("CAIRN_TEST_HELP_OMIT", "--sandbox,-m")
    monkeypatch.setenv("CAIRN_TEST_MODELS", "some-other-model")
    findings = GrokExecutor(CFG).doctor()
    assert findings  # something warned
    assert not any(f.level == "error" for f in findings)
