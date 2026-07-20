"""OpencodeExecutor — prompt via stdin, --auto under cairn's OS FS sandbox, XDG config
isolation via _extra_env, effort accepted-and-dropped (no cairn-mappable effort channel)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cairn.executors.opencode import OpencodeExecutor
from cairn.kernel.config import ExecutorConfig, TierSpec
from cairn.kernel.errors import ConfigError
from cairn.kernel.sandbox import NativeBackend
from cairn.kernel.types import Capabilities

from test_executors_base import fake_env, make_inv, read_scratch, use_fakebin


def _opencode_base(inv):
    """The per-invocation XDG base the executor derives (keyed on log_path.stem — H1)."""
    return inv.cwd / ".cairn" / f"opencode-xdg.{inv.log_path.stem}"


# Tier models are full `provider/model` strings (report §3) — resolve_model passes them through
# untouched, no opencode-specific parsing.
CFG = ExecutorConfig(
    name="opencode",
    pin_version="1.17",
    tiers={
        "reasoning": TierSpec(model="anthropic/claude-opus-4-8", effort="high"),
        "cheap": TierSpec(model="opencode/claude-sonnet-5"),
    },
)


def _invoke(tmp_path, monkeypatch, **kw):
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    inv = make_inv(tmp_path, scratch=scratch, model="anthropic/claude-opus-4-8", **kw)
    result = OpencodeExecutor(CFG).invoke(inv)
    argv, env, stdin = read_scratch(scratch)
    return result, inv, argv, env, stdin


# --------------------------------------------------------------------------- #
# argv shape / prompt delivery
# --------------------------------------------------------------------------- #


def test_argv_shape_and_prompt_on_stdin(tmp_path, monkeypatch):
    _, inv, argv, _, stdin = _invoke(tmp_path, monkeypatch, prompt="the opencode prompt", effort="high")
    assert argv[0].split("/")[-1] == "opencode"
    assert argv[1:] == [
        "run",
        "--dir", str(inv.cwd),
        "--model", "anthropic/claude-opus-4-8",
        "--format", "default",
        "--auto",
    ]
    assert stdin == "the opencode prompt"  # prompt is delivered on stdin, not argv


def test_no_positional_message_in_argv(tmp_path, monkeypatch):
    # report §2: message + stdin are COMBINED if both present — never pass a positional message.
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, prompt="do the thing")
    assert "do the thing" not in argv


def test_auto_and_format_default_present(tmp_path, monkeypatch):
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    assert "--auto" in argv
    i = argv.index("--format")
    assert argv[i + 1] == "default"


def test_dir_matches_run_dir(tmp_path, monkeypatch):
    _, inv, argv, _, _ = _invoke(tmp_path, monkeypatch)
    i = argv.index("--dir")
    assert argv[i + 1] == str(inv.cwd)


def test_cwd_is_run_dir(tmp_path, monkeypatch):
    _, inv, _, _, _ = _invoke(tmp_path, monkeypatch)
    assert (tmp_path / "scratch" / "cwd.txt").read_text() == str(inv.cwd)


# --------------------------------------------------------------------------- #
# effort — dispatch decision: no cairn-mappable channel, accepted-and-dropped
# --------------------------------------------------------------------------- #


def test_effort_never_emits_a_flag_when_set(tmp_path, monkeypatch):
    _, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort="high")
    assert "--variant" not in argv
    # Exact pin: argv is precisely the 6 tokens _build_command emits, nothing effort-shaped
    # tacked on (the tmp_path fixture dir itself can contain "effort" in its name, so this
    # checks argv shape, not a loose substring scan over the whole argv list).
    assert argv[1:] == ["run", "--dir", str((tmp_path / "run")), "--model", "anthropic/claude-opus-4-8", "--format", "default", "--auto"]


def test_effort_none_does_not_crash(tmp_path, monkeypatch):
    result, _, argv, _, _ = _invoke(tmp_path, monkeypatch, effort=None)
    assert result.exit_code == 0
    assert "--variant" not in argv


# --------------------------------------------------------------------------- #
# config isolation — XDG relocation under the run dir + hygiene env
# --------------------------------------------------------------------------- #


def test_xdg_dirs_relocated_under_run_dir_and_created(tmp_path, monkeypatch):
    _, inv, _, env, _ = _invoke(tmp_path, monkeypatch)
    base = _opencode_base(inv)
    for kind in ("config", "data", "cache", "state"):
        expected = str(base / kind)
        assert env[f"XDG_{kind.upper()}_HOME"] == expected
        assert (base / kind).is_dir()  # created idempotently by _extra_env


def test_parallel_steps_get_isolated_xdg_homes(tmp_path, monkeypatch):
    # H1 (xcli-review-quality.md): a ParallelNode runs its children concurrently on the SAME
    # executor instance, and walk.py sets inv.cwd = self.run_dir for every step — so two parallel
    # opencode steps share one cwd exactly like this. Before the fix, _extra_env derived the XDG
    # base from inv.cwd alone, so BOTH invocations below would resolve to the identical
    # `.cairn/opencode-xdg` base — one XDG_DATA_HOME/auth.json/session store shared between two
    # live opencode processes, the exact cross-session isolation the relocation exists to
    # provide. This test fails on that old code: every env_a[kind] == env_b[kind] below.
    use_fakebin(monkeypatch)
    scratch_a = tmp_path / "scratch-a"
    scratch_b = tmp_path / "scratch-b"
    inv_a = make_inv(
        tmp_path, scratch=scratch_a, model="anthropic/claude-opus-4-8",
        log_path=tmp_path / "logs" / "stepA.log",
    )
    inv_b = make_inv(
        tmp_path, scratch=scratch_b, model="opencode/claude-sonnet-5",
        log_path=tmp_path / "logs" / "stepB.log",
    )
    assert inv_a.cwd == inv_b.cwd  # both steps of one run share the run dir (walk.py:471)

    ex = OpencodeExecutor(CFG)
    ex.invoke(inv_a)
    ex.invoke(inv_b)

    argv_a, env_a, _ = read_scratch(scratch_a)
    argv_b, env_b, _ = read_scratch(scratch_b)
    for kind in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        assert env_a[kind] != env_b[kind]  # distinct homes, no shared session-store race
    # Each invocation's own --model still reaches its own child argv untouched (model rides
    # argv, not a generated file, for opencode) — the isolation gap H1 flags is the XDG base.
    assert argv_a[argv_a.index("--model") + 1] == "anthropic/claude-opus-4-8"
    assert argv_b[argv_b.index("--model") + 1] == "opencode/claude-sonnet-5"


def test_xdg_extra_env_wins_over_walker_set_state_home(tmp_path, monkeypatch):
    # The walker sets XDG_STATE_HOME ambient in inv.env for claude's gatekeys hook subprocess.
    # opencode installs no hook (installs_hooks=False), so overriding it is safe and _extra_env
    # is merged OVER inv.env by design (_cli.py::invoke).
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    env = fake_env(scratch, XDG_STATE_HOME="/somewhere/walker/set/for/gatekeys")
    inv = make_inv(tmp_path, scratch=scratch, model="anthropic/claude-opus-4-8", env=env)
    OpencodeExecutor(CFG).invoke(inv)
    _, out_env, _ = read_scratch(scratch)
    assert out_env["XDG_STATE_HOME"] == str(_opencode_base(inv) / "state")


def test_hygiene_and_claude_fallback_env_present(tmp_path, monkeypatch):
    _, _, _, env, _ = _invoke(tmp_path, monkeypatch)
    assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"
    assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    assert env["OPENCODE_DISABLE_TERMINAL_TITLE"] == "1"


def test_env_sealed_no_os_secret_leak(tmp_path, monkeypatch):
    _, _, _, env, _ = _invoke(tmp_path, monkeypatch)
    assert env["CAIRN_CANARY"] == "canary-value"
    assert "OS_ONLY_SECRET" not in env


# --------------------------------------------------------------------------- #
# STEP parsing / exit code
# --------------------------------------------------------------------------- #


def test_step_parsed_and_exit_code(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    scratch = tmp_path / "scratch"
    inv = make_inv(
        tmp_path, scratch=scratch, model="anthropic/claude-opus-4-8",
        env=fake_env(scratch, CAIRN_TEST_EXIT="4"),
    )
    result = OpencodeExecutor(CFG).invoke(inv)
    assert result.exit_code == 4
    assert result.step == {"status": "done", "summary": "fake ok", "artifacts": []}


# --------------------------------------------------------------------------- #
# capabilities / model resolution
# --------------------------------------------------------------------------- #


def test_capabilities():
    caps = OpencodeExecutor(CFG).capabilities
    # Contract rule 1 (shared brief): honest defaults; sandbox="fs" per dispatch (opencode's own
    # --auto is a policy switch, not an OS jail — report §5).
    assert caps == Capabilities(
        blocking_hooks=None, output_schema=False, session_capture=None,
        installs_hooks=False, sandbox="fs",
    )


def test_resolve_model_passthrough_provider_model_strings():
    ex = OpencodeExecutor(CFG)
    assert ex.resolve_model("reasoning", "low") == ("anthropic/claude-opus-4-8", "high")
    assert ex.resolve_model("cheap", "medium") == ("opencode/claude-sonnet-5", "medium")


def test_resolve_model_unknown_tier_raises():
    with pytest.raises(ConfigError):
        OpencodeExecutor(CFG).resolve_model("no-such-tier", "high")


# --------------------------------------------------------------------------- #
# workspace rendering
# --------------------------------------------------------------------------- #


def test_render_workspace_writes_agents_md(tmp_path):
    doctrine = tmp_path / "DOCTRINE.md"
    doctrine.write_text("opencode doctrine", encoding="utf-8")
    OpencodeExecutor(CFG).render_workspace(SimpleNamespace(root=tmp_path, doctrine=doctrine))
    assert "opencode doctrine" in (tmp_path / "AGENTS.md").read_text()
    assert not (tmp_path / "CLAUDE.md").exists()


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def test_doctor_healthy_when_version_matches_pin(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    # Hermetic: stub the sandbox primitive as present (mirrors test_executors_claude.py's
    # test_doctor_healthy_with_fake_version) so this asserts doctor's LOGIC on a healthy machine,
    # not whether the host actually has sandbox-exec/bwrap.
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    assert OpencodeExecutor(CFG).doctor() == []


def test_doctor_warns_on_pin_mismatch(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("CAIRN_TEST_VERSION", "1.9.9")
    findings = OpencodeExecutor(CFG).doctor()
    assert any(f.level == "warning" and "1.17" in f.message for f in findings)


def test_doctor_reports_missing_binary(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    findings = OpencodeExecutor(CFG).doctor()
    assert any(f.level == "error" and f.fix for f in findings)


def test_doctor_warns_when_sandbox_primitive_unavailable(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: False)
    findings = OpencodeExecutor(CFG).doctor()
    assert any(
        f.level == "warning" and "UNSANDBOXED" in f.message for f in findings
    )


# --------------------------------------------------------------------------- #
# W5b-style doctor drift checks (sub-change A pattern)
# --------------------------------------------------------------------------- #


def test_doctor_warns_when_emitted_flag_missing_from_help(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("CAIRN_TEST_HELP_OMIT", "--auto")
    findings = OpencodeExecutor(CFG).doctor()
    assert any(
        f.level == "warning" and "--auto" in f.message and "not advertised" in f.message
        for f in findings
    )


def test_doctor_no_flag_warning_on_healthy_help(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    findings = OpencodeExecutor(CFG).doctor()
    assert not any("not advertised" in f.message for f in findings)


def test_doctor_warns_when_help_fetch_fails(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("CAIRN_TEST_HELP_FAIL", "1")
    findings = OpencodeExecutor(CFG).doctor()
    assert any(f.level == "warning" and "could not run" in f.message for f in findings)
    assert sum("not advertised" in f.message for f in findings) == 0


def test_doctor_no_model_check_for_opencode(tmp_path, monkeypatch):
    # No _model_findings override — see the NOTE in opencode.py: the feasibility report has no
    # raw-captured `opencode models` output to parse against, unlike grok's. An unrecognizable
    # model string is silently not checked (never a false WARN).
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    cfg = ExecutorConfig(
        name="opencode", pin_version="1.17",
        tiers={"cheap": TierSpec(model="totally-not-a-real-model")},
    )
    findings = OpencodeExecutor(cfg).doctor()
    assert findings == []


def test_doctor_never_hard_fails_on_flag_drift(tmp_path, monkeypatch):
    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)
    monkeypatch.setenv("CAIRN_TEST_HELP_OMIT", "--auto,--model,--dir")
    findings = OpencodeExecutor(CFG).doctor()
    assert findings  # something warned
    assert not any(f.level == "error" for f in findings)


def test_doctor_survives_probe_version_spawn_error(tmp_path, monkeypatch):
    from cairn.kernel.errors import ExecutorSpawnError

    use_fakebin(monkeypatch)
    monkeypatch.setattr(NativeBackend, "available", lambda self: True)

    def _boom(name, timeout_s=15.0):
        raise ExecutorSpawnError(f"{name!r} failed to start", executable=name)

    monkeypatch.setattr("cairn.executors._cli._probe_version", _boom)
    findings = OpencodeExecutor(CFG).doctor()  # must not raise
    assert len(findings) == 1
    assert findings[0].level == "error"
    assert "failed to run" in findings[0].message
