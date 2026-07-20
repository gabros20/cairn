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
        "--sandbox", "workspace-write", "--skip-git-repo-check", "--ephemeral",
        "--ignore-user-config", "--ignore-rules",
        "-c", "sandbox_workspace_write.network_access=false",
        "-c", "model_reasoning_effort=high",
    ]
    assert stdin == "the codex prompt"  # prompt is delivered on stdin, not argv


def test_ephemeral_flag_present(tmp_path, monkeypatch):
    # W5b sub-change B (codex-F15 mirror of claude-F7): run session-less so there is nothing
    # under ~/.codex/sessions/** to capture — session_capture is None (test_capabilities).
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    assert "--ephemeral" in argv


def test_network_access_defaults_false(tmp_path, monkeypatch):
    # W5b sub-change C (codex-F5): Invocation.network defaults False — a step that doesn't
    # opt into network gets the config key stated explicitly, not left to the sandbox default.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    i = argv.index("-c")
    assert argv[i + 1] == "sandbox_workspace_write.network_access=false"


def test_network_access_true_flows_through(tmp_path, monkeypatch):
    # W5b sub-change C: a network:true step's resolved Invocation.network reaches codex's argv.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None, network=True)
    assert "sandbox_workspace_write.network_access=true" in argv
    assert "sandbox_workspace_write.network_access=false" not in argv


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
    # Exact pin of the no-effort argv so drift in this branch fails loudly too. `-c` still
    # appears once, for the unconditional network-policy override (W5b sub-change C) — only
    # the SECOND `-c model_reasoning_effort=...` pair is what effort=None omits.
    assert argv[1:] == [
        "exec", "-C", str(inv.cwd), "-m", "gpt-5.5",
        "--sandbox", "workspace-write", "--skip-git-repo-check", "--ephemeral",
        "--ignore-user-config", "--ignore-rules",
        "-c", "sandbox_workspace_write.network_access=false",
    ]
    assert not any(a.startswith("model_reasoning_effort=") for a in argv)


def test_effort_max_flows_through(tmp_path, monkeypatch):
    # W4 (claude-F11 Done-when #4): "max" must actually flow through to the emitted argv, not
    # just be accepted by the EFFORTS enum.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort="max")
    assert "model_reasoning_effort=max" in argv


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
    # blocking_hooks UNVERIFIED headless → None (doctor probes later). W5b sub-change B:
    # output_schema=False (native --output-schema exists but is not wired — the STEP sentinel
    # is the contract) and session_capture=None (codex now runs with --ephemeral, so there is
    # nothing under ~/.codex/sessions/** to capture — the old glob was dead).
    assert caps == Capabilities(
        blocking_hooks=None, output_schema=False, session_capture=None,
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


# --------------------------------------------------------------------------- #
# W5b — doctor drift checks (sub-change A).
# --------------------------------------------------------------------------- #


def test_doctor_warns_when_emitted_flag_missing_from_help(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setenv("CAIRN_TEST_HELP_OMIT", "--ephemeral")
    findings = CodexExecutor(CFG).doctor()
    assert any(
        f.level == "warning" and "--ephemeral" in f.message and "not advertised" in f.message
        for f in findings
    )


def test_doctor_no_flag_warning_on_healthy_help(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    findings = CodexExecutor(CFG).doctor()
    assert not any("not advertised" in f.message for f in findings)


def test_doctor_warns_when_help_fetch_fails(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setenv("CAIRN_TEST_HELP_FAIL", "1")
    findings = CodexExecutor(CFG).doctor()
    assert any(f.level == "warning" and "could not run" in f.message for f in findings)
    # never a crash, and never one warning per flag — just the one fetch-failure line.
    assert sum("not advertised" in f.message for f in findings) == 0


def test_doctor_never_hard_fails_on_flag_or_model_drift(tmp_path, monkeypatch):
    # All W5b drift checks are WARN-level — never promoted to an error.
    use_fakebin(monkeypatch)
    monkeypatch.setenv("CAIRN_TEST_HELP_OMIT", "--ephemeral,-c,-m")
    findings = CodexExecutor(CFG).doctor()
    assert findings  # something warned
    assert not any(f.level == "error" for f in findings)


def test_doctor_no_model_check_for_codex(tmp_path, monkeypatch):
    # W5b sub-change A.2: codex has no queryable model roster — no _model_findings override,
    # so an unrecognizable-looking model string is silently not checked (never a false WARN).
    use_fakebin(monkeypatch)
    cfg = ExecutorConfig(name="codex", pin_version="0.138", tiers={"cheap": TierSpec(model="totally-not-a-real-model")})
    findings = CodexExecutor(cfg).doctor()
    assert findings == []


def test_doctor_survives_probe_version_spawn_error(tmp_path, monkeypatch):
    # W5b sub-change A.3: the pre-existing gap — `doctor()`'s `_probe_version` call had NO
    # exception handling, so a bad-shim/ENOEXEC binary (which() resolves but Popen fails)
    # raised ExecutorSpawnError (a CairnError, post-W1) UNCAUGHT — doctor crashed instead of
    # reporting a Finding (mirrors cli.py's W5a fix). shutil.which("codex") must still resolve
    # (the fakebin dir is on PATH) so doctor reaches the probe at all.
    from cairn.kernel.errors import ExecutorSpawnError

    use_fakebin(monkeypatch)

    def _boom(name, timeout_s=15.0):
        raise ExecutorSpawnError(f"{name!r} failed to start", executable=name)

    monkeypatch.setattr("cairn.executors._cli._probe_version", _boom)
    findings = CodexExecutor(CFG).doctor()  # must not raise
    assert len(findings) == 1
    assert findings[0].level == "error"
    assert "failed to run" in findings[0].message
