"""Unit tests for the empirical hook-firing probe (cairn/kernel/hookprobe.py, C4).

The engine is driven against FAKE vendor binaries — small python scripts standing in for
``claude``/``codex``. A fake is *faithful*: in the firing modes it reads the canary's real hook
config (the one ``write_canary`` produced), runs the ``PreToolUse`` hook command (which writes the
marker), and honors/ignores the deny — so these tests exercise the recipe's canary wiring, not
just the classifier. Modes are steered by ``PROBE_FAKE_MODE`` (passed via the probe's test-only
``extra_env`` seam).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from cairn.kernel import hookprobe
from cairn.kernel.hookprobe import (
    ClaudeHookRecipe,
    CodexHookRecipe,
    ProbeResult,
    build_probe_env,
    probe,
    render,
)

# --------------------------------------------------------------------------- #
# Fake vendor CLIs.
# --------------------------------------------------------------------------- #

# A faithful fake `claude`. --version prints a canned string. Otherwise it reads PROBE_FAKE_MODE
# and the canary's real .claude/settings.json, actually RUNS the PreToolUse hook command (writing
# the marker), then decides whether the "tool" runs (creates the sidecar) per mode.
_FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, time
if "--version" in sys.argv:
    print("claude 2.1.199 (fake)"); sys.exit(0)
mode = os.environ.get("PROBE_FAKE_MODE", "fires_blocks")
cwd = os.getcwd()
sidecar = os.path.join(cwd, ".cairn-probe", "sidecar")
if mode == "auth_fail":
    print("Invalid API key. Please run /login"); sys.exit(1)
if mode == "slow":
    time.sleep(5); sys.exit(0)
if mode == "idle":
    sys.exit(0)  # the agent used no tool: no marker, no sidecar
if mode == "no_fire":
    open(sidecar, "w").close(); sys.exit(0)  # hook never runs; tool runs unguarded
# firing modes: run the real hook command from the canary settings
settings = json.load(open(os.path.join(cwd, ".claude", "settings.json")))
cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
p = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True)
blocked = '"permissionDecision": "deny"' in p.stdout or '"permissionDecision":"deny"' in p.stdout
if mode == "fires_no_block":
    blocked = False
if not blocked:
    open(sidecar, "w").close()
sys.exit(0)
'''

# A faithful fake `codex`. Same idea but reads $CODEX_HOME/hooks.json.
_FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
if "--version" in sys.argv:
    print("codex-cli 0.142.5 (fake)"); sys.exit(0)
mode = os.environ.get("PROBE_FAKE_MODE", "fires_blocks")
cwd = os.getcwd()
sidecar = os.path.join(cwd, ".cairn-probe", "sidecar")
if mode == "no_fire":
    open(sidecar, "w").close(); sys.exit(0)
home = os.environ["CODEX_HOME"]
hooks = json.load(open(os.path.join(home, "hooks.json")))
cmd = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
p = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True)
blocked = '"permissionDecision": "deny"' in p.stdout or '"permissionDecision":"deny"' in p.stdout
if mode == "fires_no_block":
    blocked = False
if not blocked:
    open(sidecar, "w").close()
sys.exit(0)
'''


@pytest.fixture
def fakebin(tmp_path: Path, monkeypatch) -> Path:
    """A bindir with fake claude/codex on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("claude", _FAKE_CLAUDE), ("codex", _FAKE_CODEX)):
        f = bindir / name
        f.write_text(body, encoding="utf-8")
        f.chmod(f.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    # Hermetic: point the codex auth-seam at an empty dir so unit tests never read ~/.codex.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-such-codex-home"))
    return bindir


def _run(recipe, mode: str, ws: Path, **kw) -> ProbeResult:
    return probe(recipe, workspace_dir=ws, extra_env={"PROBE_FAKE_MODE": mode}, **kw)


# --------------------------------------------------------------------------- #
# The four classifications (claude).
# --------------------------------------------------------------------------- #


def test_claude_fires_blocks(fakebin, tmp_path):
    r = _run(ClaudeHookRecipe(), "fires_blocks", tmp_path)
    assert r.outcome == "fires_blocks"
    assert r.executor == "claude"
    assert r.cli_version and "2.1.199" in r.cli_version


def test_claude_fires_no_block(fakebin, tmp_path):
    assert _run(ClaudeHookRecipe(), "fires_no_block", tmp_path).outcome == "fires_no_block"


def test_claude_no_fire(fakebin, tmp_path):
    assert _run(ClaudeHookRecipe(), "no_fire", tmp_path).outcome == "no_fire"


def test_claude_idle_is_inconclusive(fakebin, tmp_path):
    # Agent attempted no tool → neither file → inconclusive, not a false "no_fire".
    assert _run(ClaudeHookRecipe(), "idle", tmp_path).outcome == "inconclusive"


# --------------------------------------------------------------------------- #
# Inconclusive paths: auth, timeout, missing CLI.
# --------------------------------------------------------------------------- #


def test_auth_failure_is_inconclusive(fakebin, tmp_path):
    r = _run(ClaudeHookRecipe(), "auth_fail", tmp_path)
    assert r.outcome == "inconclusive"
    assert "auth" in r.detail.lower()


def test_timeout_is_inconclusive(fakebin, tmp_path):
    r = _run(ClaudeHookRecipe(), "slow", tmp_path, timeout_s=1)
    assert r.outcome == "inconclusive"
    assert "timed out" in r.detail


def test_missing_cli_is_inconclusive(tmp_path, monkeypatch):
    # PATH without any fake → CLI not found.
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    r = probe(ClaudeHookRecipe(), workspace_dir=tmp_path)
    assert r.outcome == "inconclusive"
    assert "not found" in r.detail


# --------------------------------------------------------------------------- #
# Codex recipe against its fake (isolated CODEX_HOME).
# --------------------------------------------------------------------------- #


def test_codex_fires_blocks(fakebin, tmp_path):
    assert _run(CodexHookRecipe(), "fires_blocks", tmp_path).outcome == "fires_blocks"


def test_codex_no_fire(fakebin, tmp_path):
    assert _run(CodexHookRecipe(), "no_fire", tmp_path).outcome == "no_fire"


# --------------------------------------------------------------------------- #
# Exit-policy matrix: (capabilities.blocking_hooks) × outcome → level.
# --------------------------------------------------------------------------- #


def _result(outcome: str) -> ProbeResult:
    return ProbeResult("claude", outcome, "d", "v", "under bypassPermissions", "deny-JSON")


@pytest.mark.parametrize(
    "blocking,outcome,level",
    [
        # True is an asserted claim: only fires_blocks is ok; a falsification is an error.
        (True, "fires_blocks", "ok"),
        (True, "fires_no_block", "error"),
        (True, "no_fire", "error"),
        (True, "no_mechanism", "error"),
        (True, "inconclusive", "warning"),
        # None: the probe decides; every concrete outcome is informational, never an error.
        (None, "fires_blocks", "info"),
        (None, "fires_no_block", "info"),
        (None, "no_fire", "info"),
        (None, "inconclusive", "warning"),
        (False, "no_fire", "info"),
    ],
)
def test_render_policy_matrix(blocking, outcome, level):
    lvl, line = render(_result(outcome), blocking)
    assert lvl == level
    assert "hook probe claude" in line


def test_render_lines_are_legible():
    assert "fires+blocks under bypassPermissions → hook-primary" in render(_result("fires_blocks"), True)[1]
    assert "did NOT fire" in render(_result("no_fire"), True)[1]
    assert render(_result("fires_blocks"), True)[1].startswith("✔")
    assert render(_result("no_fire"), True)[1].startswith("✗")


# --------------------------------------------------------------------------- #
# Env fidelity + canary hygiene.
# --------------------------------------------------------------------------- #


def test_build_probe_env_mirrors_walker_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USER", "probeuser")
    monkeypatch.setenv("LOGNAME", "probeuser")
    monkeypatch.setenv("SECRET_LEAK", "nope")  # not in the allowlist → must not pass through
    canary = tmp_path / "canary"
    env = build_probe_env(canary, tmp_path)
    # The passthrough allowlist (mirrors walk.py::_build_env) — identity + locale, no secrets.
    assert env["PATH"] == "/usr/bin"
    assert env["USER"] == "probeuser" and env["LOGNAME"] == "probeuser"
    assert "SECRET_LEAK" not in env
    # The CAIRN_* / project pointers point at the canary (it IS the project for the probe).
    assert env["CAIRN_RUN_DIR"] == str(canary)
    assert env["CAIRN_STEP"] == "doctor-hook-probe"
    assert env["CLAUDE_PROJECT_DIR"] == str(canary)


def test_canary_dir_is_cleaned_up(fakebin, tmp_path, monkeypatch):
    created: list[Path] = []
    real_mkdtemp = hookprobe.tempfile.mkdtemp

    def spy(*a, **k):
        d = real_mkdtemp(*a, **k)
        if str(k.get("prefix", "")).startswith("cairn-hookprobe"):
            created.append(Path(d))
        return d

    monkeypatch.setattr(hookprobe.tempfile, "mkdtemp", spy)
    _run(ClaudeHookRecipe(), "fires_blocks", tmp_path)
    assert created, "probe should have created a canary tempdir"
    assert not created[0].exists(), "the canary tempdir must be removed after the probe"


def test_leftover_canary_cleaned_by_retry_without_warning(fakebin, tmp_path, monkeypatch):
    """A detached vendor child that writes ONCE after the first rmtree (the observed live codex
    plugins-clone straggler) is mopped up by the single bounded-wait retry — no warning."""
    real_rmtree = shutil.rmtree
    calls = {"n": 0}

    def flaky(path, *a, **k):
        real_rmtree(path, *a, **k)
        if not Path(path).name.startswith("cairn-hookprobe-"):
            return  # only meddle with the probe's canary root, not e.g. tempfile's own dirs
        calls["n"] += 1
        if calls["n"] == 1:  # simulate the child's late write, once
            (Path(path) / "canary" / ".codex" / ".tmp" / "late").mkdir(parents=True)

    monkeypatch.setattr(hookprobe.shutil, "rmtree", flaky)
    slept: list[float] = []
    monkeypatch.setattr(hookprobe.time, "sleep", lambda s: slept.append(s))

    r = _run(ClaudeHookRecipe(), "fires_blocks", tmp_path)
    assert r.outcome == "fires_blocks"
    assert r.warnings == ()
    assert calls["n"] == 2 and slept, "expected one bounded wait + one retry"


def test_leftover_canary_survives_retry_and_warns(fakebin, tmp_path, monkeypatch):
    """A straggler that persists through the retry must surface as a legible warning naming the
    leftover path (it may contain copied auth material) — and never raise."""
    real_rmtree = shutil.rmtree
    roots: list[Path] = []

    def stubborn(path, *a, **k):
        real_rmtree(path, *a, **k)
        if not Path(path).name.startswith("cairn-hookprobe-"):
            return  # only meddle with the probe's canary root, not e.g. tempfile's own dirs
        (Path(path) / "canary" / ".codex" / ".tmp" / "straggler").mkdir(parents=True)
        if Path(path) not in roots:
            roots.append(Path(path))

    monkeypatch.setattr(hookprobe.shutil, "rmtree", stubborn)
    monkeypatch.setattr(hookprobe.time, "sleep", lambda s: None)

    r = _run(ClaudeHookRecipe(), "fires_blocks", tmp_path)
    assert r.outcome == "fires_blocks"  # the verdict itself is unaffected
    assert len(r.warnings) == 1
    assert str(roots[0]) in r.warnings[0]
    assert "auth" in r.warnings[0]  # names the risk: the leftover may hold copied auth material
    real_rmtree(roots[0], ignore_errors=True)  # tidy the deliberately-planted leftover


# --------------------------------------------------------------------------- #
# The recipes emit valid, firing hook config.
# --------------------------------------------------------------------------- #


def test_claude_recipe_emits_firing_deny_hook(tmp_path):
    canary = tmp_path / "c"
    (canary / ".cairn-probe").mkdir(parents=True)
    marker = canary / ".cairn-probe" / "marker"
    sidecar = canary / ".cairn-probe" / "sidecar"
    ClaudeHookRecipe().write_canary(canary, marker, sidecar)
    settings = json.loads((canary / ".claude" / "settings.json").read_text())
    entry = settings["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Bash"
    cmd = entry["hooks"][0]["command"]
    # Running the hook command must create the marker AND print the deny decision.
    import subprocess

    p = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True)
    assert marker.exists()
    assert '"permissionDecision": "deny"' in p.stdout or '"permissionDecision":"deny"' in p.stdout


def test_codex_recipe_emits_hooks_json_under_codex_home(tmp_path):
    canary = tmp_path / "c"
    (canary / ".cairn-probe").mkdir(parents=True)
    marker = canary / ".cairn-probe" / "marker"
    CodexHookRecipe().write_canary(canary, marker, canary / ".cairn-probe" / "sidecar")
    hooks = json.loads((canary / ".codex" / "hooks.json").read_text())
    assert "PreToolUse" in hooks["hooks"]
    assert (canary / ".codex" / "config.toml").exists()
    # CODEX_HOME is relocated to the canary, keeping the user's ~/.codex untouched.
    assert CodexHookRecipe().extra_env(canary)["CODEX_HOME"] == str(canary / ".codex")


def test_codex_invocation_mirrors_real_executor_argv(tmp_path):
    """The recipe argv must be the REAL CodexExecutor argv + the probe-only trust-bypass flag.

    Pinned by construction (not a copied list) so drift in either direction — the executor
    changing shape (as it did when codex-cli 0.142.5 dropped ``-a/--ask-for-approval``) or the
    recipe growing stale — fails this test loudly."""
    from cairn.executors.codex import CodexExecutor
    from cairn.kernel.config import ExecutorConfig
    from cairn.kernel.types import Invocation

    inv = Invocation(
        prompt_file=tmp_path / "p.md", model="gpt-5.5", effort=None, cwd=tmp_path,
        env={}, timeout_s=60, log_path=tmp_path / "l.log", return_schema=tmp_path / "s.json",
    )
    exec_argv, exec_stdin = CodexExecutor(ExecutorConfig(name="codex"))._build_command(inv, "PROMPT")
    recipe_argv, recipe_stdin = CodexHookRecipe().build_invocation(tmp_path, "PROMPT", "gpt-5.5")

    assert recipe_argv == exec_argv + ["--dangerously-bypass-hook-trust"]
    assert recipe_stdin == exec_stdin == "PROMPT"
    # The two live-verified 0.142.5 facts, asserted explicitly so a regression names itself:
    assert "-a" not in recipe_argv  # `-a/--ask-for-approval` no longer exists on `codex exec`
    assert "--skip-git-repo-check" in recipe_argv  # the canary tempdir is not a git repo


def test_codex_recipe_default_model_is_gpt_5_5():
    # Live-verified: this ChatGPT account rejects every -mini/-codex model variant with a 400;
    # only gpt-5.5 is accepted (documented in tests/live/workspace-codex/cairn.toml).
    assert CodexHookRecipe().model == "gpt-5.5"


# --------------------------------------------------------------------------- #
# Codex auth seam: copy auth.json (ONLY) into the canary CODEX_HOME when present.
# --------------------------------------------------------------------------- #


def _write_real_codex_home(home: Path, *, with_auth: bool) -> None:
    home.mkdir(parents=True, exist_ok=True)
    if with_auth:
        (home / "auth.json").write_text('{"token": "real-auth-material"}', encoding="utf-8")
    # Real-home config that must NEVER leak into the canary (isolation is the point).
    (home / "hooks.json").write_text('{"hooks": {"Stop": [{"hooks": []}]}}', encoding="utf-8")
    (home / "config.toml").write_text('model = "user-model"\n', encoding="utf-8")


def _codex_canary(tmp_path: Path) -> tuple[Path, Path, Path]:
    canary = tmp_path / "canary"
    (canary / ".cairn-probe").mkdir(parents=True)
    return canary, canary / ".cairn-probe" / "marker", canary / ".cairn-probe" / "sidecar"


def test_codex_auth_present_is_copied_alone(tmp_path, monkeypatch):
    real_home = tmp_path / "realhome" / ".codex"
    _write_real_codex_home(real_home, with_auth=True)
    monkeypatch.setenv("CODEX_HOME", str(real_home))

    canary, marker, sidecar = _codex_canary(tmp_path)
    CodexHookRecipe().write_canary(canary, marker, sidecar)

    canary_home = canary / ".codex"
    # Auth material copied — a live probe can authenticate…
    assert (canary_home / "auth.json").read_text() == '{"token": "real-auth-material"}'
    # …but ONLY auth: the canary keeps its own probe hooks.json and empty config.toml.
    hooks = json.loads((canary_home / "hooks.json").read_text())
    assert "PreToolUse" in hooks["hooks"] and "Stop" not in hooks["hooks"]
    assert (canary_home / "config.toml").read_text() == ""


def test_codex_auth_absent_leaves_canary_unauthenticated(tmp_path, monkeypatch):
    real_home = tmp_path / "realhome" / ".codex"
    _write_real_codex_home(real_home, with_auth=False)
    monkeypatch.setenv("CODEX_HOME", str(real_home))

    canary, marker, sidecar = _codex_canary(tmp_path)
    CodexHookRecipe().write_canary(canary, marker, sidecar)
    assert not (canary / ".codex" / "auth.json").exists()  # → inconclusive live, as today


def test_codex_auth_defaults_to_home_dot_codex(tmp_path, monkeypatch):
    # Without $CODEX_HOME the real home is ~/.codex (expanduser honors $HOME).
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    _write_real_codex_home(tmp_path / "fakehome" / ".codex", with_auth=True)

    canary, marker, sidecar = _codex_canary(tmp_path)
    CodexHookRecipe().write_canary(canary, marker, sidecar)
    assert (canary / ".codex" / "auth.json").exists()


def test_codex_copied_auth_dies_with_the_canary(fakebin, tmp_path, monkeypatch):
    real_home = tmp_path / "realhome" / ".codex"
    _write_real_codex_home(real_home, with_auth=True)
    monkeypatch.setenv("CODEX_HOME", str(real_home))

    created: list[Path] = []
    real_mkdtemp = hookprobe.tempfile.mkdtemp

    def spy(*a, **k):
        d = real_mkdtemp(*a, **k)
        created.append(Path(d))
        return d

    monkeypatch.setattr(hookprobe.tempfile, "mkdtemp", spy)
    r = _run(CodexHookRecipe(), "fires_blocks", tmp_path)
    assert r.outcome == "fires_blocks"
    assert created and not created[0].exists(), "canary (incl. copied auth.json) must be rmtree'd"
    # And the real home was never touched.
    assert (real_home / "auth.json").read_text() == '{"token": "real-auth-material"}'


def test_claude_invocation_carries_bypass_permissions(tmp_path):
    argv, stdin_text = ClaudeHookRecipe().build_invocation(tmp_path, "PROMPT", "haiku")
    assert argv[0] == "claude" and stdin_text is None
    assert "--permission-mode" in argv and "bypassPermissions" in argv
    assert "haiku" in argv


# --------------------------------------------------------------------------- #
# Doctor wiring: probe lines appear only with probe_hooks=True.
# --------------------------------------------------------------------------- #


def _hello_ws(tmp_path):
    from cairn.kernel import newkit

    return newkit.new_workspace("demo", tmp_path)


def test_doctor_probe_lines_only_with_flag(tmp_path, monkeypatch):
    ws = _hello_ws(tmp_path)
    # Stub the probe so this stays hermetic (no subprocess); assert wiring, not mechanics.
    monkeypatch.setattr(
        hookprobe, "probe",
        lambda recipe, **kw: ProbeResult("claude", "fires_blocks", "d", "v", "under bypassPermissions", "deny"),
    )
    from cairn.kernel.doctor import run_doctor

    lines_off: list[str] = []
    run_doctor(ws, probe_hooks=False, out=lines_off.append)
    assert not any("hook probe" in ln for ln in lines_off)

    lines_on: list[str] = []
    run_doctor(ws, probe_hooks=True, out=lines_on.append)
    assert any("hook probe claude" in ln and "fires+blocks" in ln for ln in lines_on)


def test_doctor_probe_falsification_fails_exit(tmp_path, monkeypatch):
    ws = _hello_ws(tmp_path)
    # blocking_hooks=True but the probe says hooks don't fire → error → non-zero exit.
    monkeypatch.setattr(
        hookprobe, "probe",
        lambda recipe, **kw: ProbeResult("claude", "no_fire", "d", "v", "under bypassPermissions", "deny"),
    )
    from cairn.kernel.doctor import run_doctor
    from cairn.kernel.types import ExitCode

    lines: list[str] = []
    rc = run_doctor(ws, probe_hooks=True, out=lines.append)
    assert rc == int(ExitCode.CONFIG)
    assert any("✗ hook probe claude" in ln for ln in lines)


def test_doctor_prints_probe_warnings(tmp_path, monkeypatch):
    ws = _hello_ws(tmp_path)
    leftover = "canary dir left behind at /tmp/cairn-hookprobe-x (it may contain copied auth material)"
    monkeypatch.setattr(
        hookprobe, "probe",
        lambda recipe, **kw: ProbeResult(
            "claude", "fires_blocks", "d", "v", "under bypassPermissions", "deny",
            warnings=(leftover,),
        ),
    )
    from cairn.kernel.doctor import run_doctor
    from cairn.kernel.types import ExitCode

    lines: list[str] = []
    rc = run_doctor(ws, probe_hooks=True, out=lines.append)
    assert rc == int(ExitCode.OK)  # a cleanup warning never fails the exit
    assert any(ln.startswith("  !") and leftover in ln for ln in lines)


def test_doctor_probe_inconclusive_only_warns(tmp_path, monkeypatch):
    ws = _hello_ws(tmp_path)
    monkeypatch.setattr(
        hookprobe, "probe",
        lambda recipe, **kw: ProbeResult("claude", "inconclusive", "no auth", "v", "under bypassPermissions", "deny"),
    )
    from cairn.kernel.doctor import run_doctor
    from cairn.kernel.types import ExitCode

    lines: list[str] = []
    rc = run_doctor(ws, probe_hooks=True, out=lines.append)
    assert rc == int(ExitCode.OK)  # inconclusive never fails the exit
    assert any("inconclusive" in ln for ln in lines)
