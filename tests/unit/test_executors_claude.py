"""ClaudeExecutor — prompt on stdin, config isolation, --effort omitted when None, blocking hooks."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cairn.kernel.gatekeys import guard_manifest_path
from cairn.kernel.sandbox import NativeBackend

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


def test_argv_shape_prompt_on_stdin(tmp_path, monkeypatch):
    _, inv, argv, _, stdin = _invoke(tmp_path, monkeypatch, prompt="hello prompt", model="opus", effort="high")
    assert argv[0].split("/")[-1] == "claude"  # argv[0] is the PATH-resolved binary
    assert argv[1:] == [
        "-p",
        "--model", "opus", "--effort", "high",
        "--output-format", "text",
        "--permission-mode", "bypassPermissions",
        "--setting-sources", "project",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    assert "hello prompt" not in argv  # W4 (claude-F2): not exposed on the argv/ps surface
    assert stdin == "hello prompt"  # claude gets the prompt on stdin, not as an arg


def test_headless_permission_mode_is_bypass(tmp_path, monkeypatch):
    # Without a permission mode a headless `claude -p` refuses every tool use ("I need your
    # permission…") and never writes its artifact. cairn's own guards (blocking PreToolUse
    # hooks) are the enforcement layer, so the executor runs claude fully non-interactive.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch)
    i = argv.index("--permission-mode")
    assert argv[i : i + 2] == ["--permission-mode", "bypassPermissions"]


def test_config_isolation_flags_present(tmp_path, monkeypatch):
    # W4 (codex-F6/grok-F6/claude's half of F5): seal the process from ambient user config —
    # keeps the run-dir hook (the "project" source) while dropping the user's settings, and
    # ignores any ambient MCP servers.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch)
    assert "--setting-sources" in argv
    i = argv.index("--setting-sources")
    assert argv[i + 1] == "project"
    assert "--strict-mcp-config" in argv


def test_no_session_persistence_present(tmp_path, monkeypatch):
    # W4 (claude-F7): retire the dead session-capture path.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch)
    assert "--no-session-persistence" in argv


def test_effort_pair_omitted_when_none(tmp_path, monkeypatch):
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    assert "--effort" not in argv


def test_effort_max_flows_through(tmp_path, monkeypatch):
    # W4 (claude-F11 Done-when #4): "max" must actually flow through to the emitted argv, not
    # just be accepted by the EFFORTS enum.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort="max")
    i = argv.index("--effort")
    assert argv[i + 1] == "max"


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
    # W4 (claude-F7): session_capture retired to None — --no-session-persistence means there
    # are no transcripts to capture, and nothing ever consumed the old glob.
    # C8/W3c: sandbox="fs" — claude's process is wrapped in the OS filesystem sandbox.
    assert caps == Capabilities(
        blocking_hooks=True, output_schema=False, session_capture=None,
        installs_hooks=True, sandbox="fs",
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
    # Hermetic: stub the sandbox primitive as present so this asserts doctor's logic on a
    # healthy machine, not whether the host has sandbox-exec/bwrap (a Linux runner without
    # bwrap correctly WARNs — both branches are covered hermetically in test_sandbox.py's
    # test_doctor_warns_when_fs_posture_unavailable / test_doctor_silent_when_available_or_off).
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    assert ClaudeExecutor(CFG).doctor() == []


def test_doctor_reports_missing_binary(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    findings = ClaudeExecutor(CFG).doctor()
    assert any(f.level == "error" and f.fix for f in findings)


# --------------------------------------------------------------------------- #
# W5b — doctor drift checks (sub-change A).
# --------------------------------------------------------------------------- #


def test_doctor_warns_when_emitted_flag_missing_from_help(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setenv("CAIRN_TEST_HELP_OMIT", "--setting-sources")
    findings = ClaudeExecutor(CFG).doctor()
    assert any(
        f.level == "warning" and "--setting-sources" in f.message and "not advertised" in f.message
        for f in findings
    )


def test_doctor_no_flag_warning_on_healthy_help(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    findings = ClaudeExecutor(CFG).doctor()
    assert not any("not advertised" in f.message for f in findings)


def test_doctor_aliases_and_dated_ids_pass_model_check(tmp_path, monkeypatch):
    # CFG uses "opus" (alias) and "haiku" (alias) — both recognized, no warning.
    use_fakebin(monkeypatch)
    findings = ClaudeExecutor(CFG).doctor()
    assert not any("not a recognized claude alias" in f.message for f in findings)


def test_doctor_warns_on_unrecognized_model_string(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    cfg = ExecutorConfig(name="claude", tiers={"cheap": TierSpec(model="gpt-4o")})
    findings = ClaudeExecutor(cfg).doctor()
    assert any(
        f.level == "warning" and "gpt-4o" in f.message and "not a recognized claude alias" in f.message
        for f in findings
    )


def test_doctor_accepts_dated_model_ids(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    cfg = ExecutorConfig(
        name="claude",
        tiers={
            "reasoning": TierSpec(model="claude-opus-4-8"),
            "balanced": TierSpec(model="claude-sonnet-5"),
            "cheap": TierSpec(model="claude-haiku-4-5-20251001"),
        },
    )
    findings = ClaudeExecutor(cfg).doctor()
    assert not any("not a recognized claude alias" in f.message for f in findings)


def test_doctor_never_hard_fails_on_drift(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setenv("CAIRN_TEST_HELP_OMIT", "--setting-sources,--model")
    cfg = ExecutorConfig(name="claude", tiers={"cheap": TierSpec(model="not-a-claude-model")})
    findings = ClaudeExecutor(cfg).doctor()
    assert findings  # something warned
    assert not any(f.level == "error" for f in findings)


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


def _hook_guard(name="no-media", *, tool="bash", command="brease*", enforce=("hook", "post")):
    from pathlib import Path as _Path

    from cairn.kernel.plan import GuardDecl

    return GuardDecl(
        name=name,
        match_tool=tool,
        match_command=command,
        check=_Path("/nonexistent/check.py"),
        enforce=enforce,
        on_error="deny",
        when=None,
    )


def test_install_guards_no_hook_guards_writes_nothing(tmp_path):
    # A guard enforced only at shim/post — NOT hook — installs no claude hook.
    ws = tmp_path / "ws"  # install_guards receives workspace_dir as a Path (as the walker passes)
    run_dir = tmp_path / "run"
    guards = [_hook_guard(enforce=("shim", "post"))]
    assert ClaudeExecutor(CFG).install_guards(guards, ws, run_dir) is None
    assert not (run_dir / ".claude" / "settings.json").exists()
    assert not guard_manifest_path(run_dir, "hook").exists()


def test_install_guards_writes_settings_and_manifest(tmp_path):
    ws = tmp_path / "ws"  # install_guards receives workspace_dir as a Path (as the walker passes)
    run_dir = tmp_path / "run"
    guards = [_hook_guard(name="no-media", command="brease* createMedia*")]
    ClaudeExecutor(CFG).install_guards(guards, ws, run_dir)

    # The manifest lives OUTSIDE the run dir (protected, agent-unwritable) and is signed.
    manifest_path = guard_manifest_path(run_dir, "hook")
    assert str(run_dir) not in str(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    assert "no-media" in manifest["guards"]
    assert manifest["workspace_dir"] == str(tmp_path / "ws")
    assert isinstance(manifest["mac"], str) and manifest["mac"]  # authenticated

    settings = json.loads((run_dir / ".claude" / "settings.json").read_text())
    entries = settings["hooks"]["PreToolUse"]
    assert len(entries) == 1
    assert entries[0]["matcher"] == "Bash"
    command = entries[0]["hooks"][0]["command"]
    assert "cairn.kernel.guards --hook-check" in command
    assert "no-media" in command
    # C9: the manifest path is no longer baked into the hook command — the hook subprocess
    # INHERITS CAIRN_HOOK_MANIFEST from claude's env (walker-set per invocation), falling back to
    # guard_manifest_path(CAIRN_RUN_DIR, "hook") — exactly manifest_path above — when unset.
    assert str(manifest_path) not in command
    assert "CAIRN_HOOK_MANIFEST" not in command


def test_install_guards_is_idempotent(tmp_path):
    ws = tmp_path / "ws"  # install_guards receives workspace_dir as a Path (as the walker passes)
    run_dir = tmp_path / "run"
    guards = [_hook_guard()]
    ex = ClaudeExecutor(CFG)
    ex.install_guards(guards, ws, run_dir)
    settings_path = run_dir / ".claude" / "settings.json"
    first = settings_path.read_text()
    mtime = settings_path.stat().st_mtime_ns
    ex.install_guards(guards, ws, run_dir)  # second call: byte-identical, no rewrite
    assert settings_path.read_text() == first
    assert settings_path.stat().st_mtime_ns == mtime


def test_install_guards_merges_into_existing_settings(tmp_path):
    # A pre-existing settings.json with an unrelated key + a foreign PreToolUse hook must survive.
    run_dir = tmp_path / "run"
    (run_dir / ".claude").mkdir(parents=True)
    (run_dir / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo keep"}]}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    ws = tmp_path / "ws"  # install_guards receives workspace_dir as a Path (as the walker passes)
    ClaudeExecutor(CFG).install_guards([_hook_guard()], ws, run_dir)

    settings = json.loads((run_dir / ".claude" / "settings.json").read_text())
    assert settings["model"] == "opus"  # unrelated key preserved
    commands = [h["command"] for e in settings["hooks"]["PreToolUse"] for h in e["hooks"]]
    assert "echo keep" in commands  # foreign hook preserved
    assert any("--hook-check" in c for c in commands)  # our hook added
