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
    GrokHookRecipe,
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


# A faithful fake `grok`. Reads $GROK_HOME/hooks/*.json (the GLOBAL hook the recipe installed),
# runs the PreToolUse command (writing the marker), and blocks on deny-JSON OR exit 2 — grok's
# two documented block signals. --version prints a canned string.
_FAKE_GROK = r'''#!/usr/bin/env python3
import glob, json, os, subprocess, sys, time
if "--version" in sys.argv:
    print("grok 0.2.82 (fake)"); sys.exit(0)
mode = os.environ.get("PROBE_FAKE_MODE", "fires_blocks")
cwd = os.getcwd()
sidecar = os.path.join(cwd, ".cairn-probe", "sidecar")
if mode == "auth_fail":
    print("Not logged in. Please run grok login"); sys.exit(1)
if mode == "slow":
    time.sleep(5); sys.exit(0)
if mode == "idle":
    sys.exit(0)  # the agent used no tool: no marker, no sidecar
if mode == "no_fire":
    open(sidecar, "w").close(); sys.exit(0)  # hook never runs; tool runs unguarded
home = os.environ["GROK_HOME"]
hookfile = glob.glob(os.path.join(home, "hooks", "*.json"))[0]
hooks = json.load(open(hookfile))
cmd = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
p = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True)
blocked = (
    '"decision": "deny"' in p.stdout or '"decision":"deny"' in p.stdout or p.returncode == 2
)
if mode == "fires_no_block":
    blocked = False
if not blocked:
    open(sidecar, "w").close()
sys.exit(0)
'''


@pytest.fixture
def fakebin(tmp_path: Path, monkeypatch) -> Path:
    """A bindir with fake claude/codex/grok on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("claude", _FAKE_CLAUDE), ("codex", _FAKE_CODEX), ("grok", _FAKE_GROK)):
        f = bindir / name
        f.write_text(body, encoding="utf-8")
        f.chmod(f.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    # Hermetic: point the codex/grok auth-seams at empty dirs so unit tests never read the
    # user's real ~/.codex / ~/.grok.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-such-codex-home"))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "no-such-grok-home"))
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
# grok recipe against its fake (relocated GROK_HOME, global hook). All four
# classifications flow through the engine unchanged for grok.
# --------------------------------------------------------------------------- #


def test_grok_registered_in_recipes():
    assert isinstance(hookprobe.RECIPES["grok"], GrokHookRecipe)


def test_grok_fires_blocks(fakebin, tmp_path):
    r = _run(GrokHookRecipe(), "fires_blocks", tmp_path)
    assert r.outcome == "fires_blocks"
    assert r.executor == "grok"
    assert r.cli_version and "0.2.82" in r.cli_version


def test_grok_fires_no_block(fakebin, tmp_path):
    assert _run(GrokHookRecipe(), "fires_no_block", tmp_path).outcome == "fires_no_block"


def test_grok_no_fire(fakebin, tmp_path):
    assert _run(GrokHookRecipe(), "no_fire", tmp_path).outcome == "no_fire"


def test_grok_idle_is_inconclusive(fakebin, tmp_path):
    assert _run(GrokHookRecipe(), "idle", tmp_path).outcome == "inconclusive"


def test_grok_auth_failure_is_inconclusive(fakebin, tmp_path):
    r = _run(GrokHookRecipe(), "auth_fail", tmp_path)
    assert r.outcome == "inconclusive"
    assert "auth" in r.detail.lower()


def test_grok_exit_policy_true_falsified_is_error():
    # GrokExecutor.capabilities.blocking_hooks is True, so a live no_fire/fires_no_block verdict
    # is a FALSIFICATION → doctor error (falsified design claim). This must not be softened.
    for outcome in ("no_fire", "fires_no_block"):
        lvl, _ = render(
            ProbeResult("grok", outcome, "d", "v", "under bypassPermissions", "m"), True
        )
        assert lvl == "error"
    lvl_ok, _ = render(
        ProbeResult("grok", "fires_blocks", "d", "v", "under bypassPermissions", "m"), True
    )
    assert lvl_ok == "ok"


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


def test_env_passthrough_matches_walker_baseline_tuple():
    """Full-tuple lockstep guard: ``_ENV_PASSTHROUGH`` must equal the walker's deny-by-default
    baseline in walk.py::_Walk._build_env. The walker inlines its tuple (no named constant, and
    walk.py is not this package's to edit), so the guard reads it from the source of the ONE
    method that owns it — if that loop is reshaped or renamed, this fails loudly and the
    lockstep must be re-verified by hand."""
    import inspect
    import re

    from cairn.kernel import walk

    src = inspect.getsource(walk._Walk._build_env)
    m = re.search(r"for key in \(([^)]*)\):", src)
    assert m, "walk.py _build_env baseline loop not found — re-verify the env lockstep"
    walker_keys = tuple(s.strip().strip("\"'") for s in m.group(1).split(",") if s.strip())
    assert walker_keys == hookprobe._ENV_PASSTHROUGH


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


def test_codex_auth_copy_oserror_degrades_to_unauthenticated(tmp_path, monkeypatch):
    # A failing copy (disk/permissions) must degrade to the unauthenticated path — the live
    # probe then reports inconclusive — and must NEVER crash write_canary.
    real_home = tmp_path / "realhome" / ".codex"
    _write_real_codex_home(real_home, with_auth=True)
    monkeypatch.setenv("CODEX_HOME", str(real_home))
    monkeypatch.setattr(
        hookprobe.shutil, "copyfile",
        lambda src, dst: (_ for _ in ()).throw(OSError("disk says no")),
    )

    canary, marker, sidecar = _codex_canary(tmp_path)
    CodexHookRecipe().write_canary(canary, marker, sidecar)  # must not raise
    assert not (canary / ".codex" / "auth.json").exists()
    # The rest of the canary is intact — only the auth material is missing.
    assert (canary / ".codex" / "hooks.json").exists()


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
# grok recipe: emits a firing GLOBAL deny hook under a relocated GROK_HOME, and its
# invocation mirrors the real executor by construction.
# --------------------------------------------------------------------------- #


def _grok_canary(tmp_path: Path) -> tuple[Path, Path, Path]:
    canary = tmp_path / "canary"
    (canary / ".cairn-probe").mkdir(parents=True)
    return canary, canary / ".cairn-probe" / "marker", canary / ".cairn-probe" / "sidecar"


def test_grok_recipe_emits_firing_global_deny_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "no-such-grok-home"))  # hermetic auth seam
    canary, marker, sidecar = _grok_canary(tmp_path)
    GrokHookRecipe().write_canary(canary, marker, sidecar)

    # The hook is GLOBAL: it lives under $GROK_HOME/hooks/ (always trusted), not <project>/.grok.
    home = canary / ".grok"
    hookfile = home / "hooks" / "cairn-probe.json"
    hook = json.loads(hookfile.read_text())
    entry = hook["hooks"]["PreToolUse"][0]
    # NO matcher → matches every tool. grok's composer-agent shell tool is 'Shell', not 'Bash', so
    # a "Bash" matcher would silently miss it (live-verified) → a false no_fire.
    assert "matcher" not in entry
    cmd = entry["hooks"][0]["command"]
    # ALL claude/cursor compat cells disabled so the user's ~/.claude MCP servers / rules / hooks
    # can't load into the canary (they stall headless shutdown + interfere).
    cfg = (home / "config.toml").read_text()
    assert "[compat.claude]" in cfg and "[compat.cursor]" in cfg
    assert "mcps = false" in cfg and "hooks = false" in cfg
    # GROK_HOME is relocated into the canary.
    assert GrokHookRecipe().extra_env(canary)["GROK_HOME"] == str(home)
    # Running the hook command must create the marker, print grok's deny-JSON, AND exit 2.
    import subprocess

    p = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True)
    assert marker.exists()
    assert '"decision": "deny"' in p.stdout or '"decision":"deny"' in p.stdout
    assert p.returncode == 2


def test_grok_invocation_mirrors_real_executor_argv(tmp_path):
    """The recipe argv must equal the REAL GrokExecutor argv EXACTLY — no probe-only extra flag,
    because the hook is global (always trusted) so there is no folder-trust to bypass.

    Pinned by construction (not a copied list): drift in either direction — the executor changing
    shape or the recipe growing stale — fails this test loudly."""
    from cairn.executors.grok import GrokExecutor
    from cairn.kernel.config import ExecutorConfig
    from cairn.kernel.types import Invocation

    canary, _marker, _sidecar = _grok_canary(tmp_path)
    # The recipe writes the prompt to <canary>/.cairn-probe/prompt.md and points --prompt-file
    # there; the executor references inv.prompt_file — so pin the mirror with that exact path.
    prompt_file = canary / ".cairn-probe" / "prompt.md"
    inv = Invocation(
        prompt_file=prompt_file, model="grok-composer-2.5-fast", effort="low", cwd=canary,
        env={}, timeout_s=60, log_path=tmp_path / "l.log", return_schema=tmp_path / "s.json",
    )
    exec_argv, exec_stdin = GrokExecutor(ExecutorConfig(name="grok"))._build_command(inv, "PROMPT")
    recipe_argv, recipe_stdin = GrokHookRecipe().build_invocation(
        canary, "PROMPT", "grok-composer-2.5-fast"
    )

    assert recipe_argv == exec_argv  # exact mirror — grok needs no trust-bypass extra
    assert recipe_stdin == exec_stdin is None  # prompt via --prompt-file; stdin is dead headless
    # The recipe actually materialized the prompt file it references (self-contained).
    assert prompt_file.read_text() == "PROMPT"
    # The two live-verified 0.2.82 facts, asserted so a regression names itself:
    assert "--prompt-file" in recipe_argv  # stdin is not read headlessly
    assert "text" not in recipe_argv and "plain" in recipe_argv  # --output-format plain, not text


def test_grok_recipe_default_model_is_composer_2_5_fast():
    # On this install only grok-composer-2.5-fast and grok-build exist (models_cache.json).
    assert GrokHookRecipe().model == "grok-composer-2.5-fast"
    assert GrokHookRecipe().effort == "low"


# --- grok auth seam: copy auth.json (ONLY) into the canary GROK_HOME when present. --------- #


def _write_real_grok_home(home: Path, *, with_auth: bool) -> None:
    home.mkdir(parents=True, exist_ok=True)
    if with_auth:
        (home / "auth.json").write_text('{"token": "real-grok-auth"}', encoding="utf-8")
    # Real-home config that must NEVER leak into the canary (isolation is the point).
    (home / "config.toml").write_text('[models]\ndefault = "user-model"\n', encoding="utf-8")
    (home / "hooks").mkdir(exist_ok=True)
    (home / "hooks" / "user.json").write_text('{"hooks": {"Stop": [{"hooks": []}]}}', encoding="utf-8")


def test_grok_auth_present_is_copied_alone(tmp_path, monkeypatch):
    real_home = tmp_path / "realhome" / ".grok"
    _write_real_grok_home(real_home, with_auth=True)
    monkeypatch.setenv("GROK_HOME", str(real_home))

    canary, marker, sidecar = _grok_canary(tmp_path)
    GrokHookRecipe().write_canary(canary, marker, sidecar)

    canary_home = canary / ".grok"
    # Auth material copied — a live probe can authenticate…
    assert (canary_home / "auth.json").read_text() == '{"token": "real-grok-auth"}'
    # …but ONLY auth: the canary keeps its own probe hook + compat-disabling config, not the
    # user's Stop hook or default model.
    assert (canary_home / "hooks" / "cairn-probe.json").exists()
    assert not (canary_home / "hooks" / "user.json").exists()
    assert "user-model" not in (canary_home / "config.toml").read_text()


def test_grok_auth_absent_leaves_canary_unauthenticated(tmp_path, monkeypatch):
    real_home = tmp_path / "realhome" / ".grok"
    _write_real_grok_home(real_home, with_auth=False)
    monkeypatch.setenv("GROK_HOME", str(real_home))

    canary, marker, sidecar = _grok_canary(tmp_path)
    GrokHookRecipe().write_canary(canary, marker, sidecar)
    assert not (canary / ".grok" / "auth.json").exists()  # → inconclusive live, as codex


def test_grok_auth_defaults_to_home_dot_grok(tmp_path, monkeypatch):
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    _write_real_grok_home(tmp_path / "fakehome" / ".grok", with_auth=True)

    canary, marker, sidecar = _grok_canary(tmp_path)
    GrokHookRecipe().write_canary(canary, marker, sidecar)
    assert (canary / ".grok" / "auth.json").exists()


def test_grok_auth_copy_oserror_degrades_to_unauthenticated(tmp_path, monkeypatch):
    real_home = tmp_path / "realhome" / ".grok"
    _write_real_grok_home(real_home, with_auth=True)
    monkeypatch.setenv("GROK_HOME", str(real_home))
    monkeypatch.setattr(
        hookprobe.shutil, "copyfile",
        lambda src, dst: (_ for _ in ()).throw(OSError("disk says no")),
    )

    canary, marker, sidecar = _grok_canary(tmp_path)
    GrokHookRecipe().write_canary(canary, marker, sidecar)  # must not raise
    assert not (canary / ".grok" / "auth.json").exists()
    assert (canary / ".grok" / "hooks" / "cairn-probe.json").exists()  # rest of canary intact


def test_grok_copied_auth_dies_with_the_canary(fakebin, tmp_path, monkeypatch):
    real_home = tmp_path / "realhome" / ".grok"
    _write_real_grok_home(real_home, with_auth=True)
    monkeypatch.setenv("GROK_HOME", str(real_home))

    created: list[Path] = []
    real_mkdtemp = hookprobe.tempfile.mkdtemp

    def spy(*a, **k):
        d = real_mkdtemp(*a, **k)
        created.append(Path(d))
        return d

    monkeypatch.setattr(hookprobe.tempfile, "mkdtemp", spy)
    r = _run(GrokHookRecipe(), "fires_blocks", tmp_path)
    assert r.outcome == "fires_blocks"
    assert created and not created[0].exists(), "canary (incl. copied auth.json) must be rmtree'd"
    assert (real_home / "auth.json").read_text() == '{"token": "real-grok-auth"}'  # real home untouched


# --------------------------------------------------------------------------- #
# Doctor wiring: probe lines appear only with probe_hooks=True.
# --------------------------------------------------------------------------- #


def _hello_ws(tmp_path):
    from cairn.kernel import newkit

    return newkit.new_workspace("demo", tmp_path)


def _healthy_executor_check(monkeypatch):
    # The workspace's default executor is claude; its health check is environment-dependent
    # (the CLI is absent on CI runners, present on the dev machine) and would contaminate the
    # exit code. Pin it healthy — the probe's exit contribution is what these tests assert.
    from cairn.kernel import doctor

    monkeypatch.setattr(doctor, "_doctor_executor", lambda *a, **kw: 0)


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
    _healthy_executor_check(monkeypatch)
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
    _healthy_executor_check(monkeypatch)
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
    _healthy_executor_check(monkeypatch)
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
