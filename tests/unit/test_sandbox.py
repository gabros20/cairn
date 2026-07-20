"""The OS filesystem sandbox (C8 / W3c) — cairn.kernel.sandbox + its executor wiring.

Two layers of coverage:

* **Pure profile/argv generation** (any OS): the SBPL string (macOS) and the bwrap/landrun argv
  (Linux) a :class:`NativeBackend` produces for a given :class:`SandboxPolicy`, plus the wrapper's
  posture gating, degradation, and realpath resolution. These never touch a real sandbox.
* **Real containment** (skipif no primitive): a wrapped command's writes/reads are actually confined
  — the host is denied, run_dir/workspace/TMPDIR are writable, the gatekeys dir is READ-only
  (the W3c close), and the guard shim still DENIES through the wrap (invariant 2).

The containment tests need no real ``claude`` binary — they drive ``/bin/sh``/``python`` through the
same backend the executor uses. ``skipif`` keeps CI green on a machine without the primitive.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cairn.executors._cli import CliExecutor
from cairn.kernel.config import ExecutorConfig
from cairn.kernel.gatekeys import guard_manifest_path, guard_manifests_dir
from cairn.kernel.guards import build_shims
from cairn.kernel.plan import GuardDecl
from cairn.kernel.sandbox import (
    NativeBackend,
    SandboxBackend,
    SandboxPolicy,
    SandboxUnavailable,
    SandboxWrapper,
    build_wrapper,
)
from cairn.kernel.types import Capabilities

from test_executors_base import make_inv

_BACKEND = NativeBackend()
_HAVE_SANDBOX = _BACKEND.available()
requires_sandbox = pytest.mark.skipif(
    not _HAVE_SANDBOX, reason="no functional OS filesystem sandbox primitive on this machine"
)
CHECKS = Path(__file__).parent / "fixtures" / "guard-checks"


def _policy(rw=(), ro=(), tmp=(), network=True) -> SandboxPolicy:
    return SandboxPolicy.build(
        rw_paths=list(rw), ro_paths=list(ro), tmp_paths=list(tmp), network=network
    )


def _run_wrapped(script: str, policy: SandboxPolicy, *, env: dict | None = None):
    """Run ``/bin/sh -c script`` wrapped by the real backend; return the CompletedProcess."""
    argv = _BACKEND.wrap(["/bin/sh", "-c", script], policy)
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, env=run_env)


# --------------------------------------------------------------------------- #
# SandboxPolicy.build — realpath resolution + dedup (any OS)
# --------------------------------------------------------------------------- #


def test_policy_realpath_resolves_symlinks(tmp_path):
    # Symlink-widening guard: a path passed to the policy is stored as its REAL target, so the
    # generated scope is exactly what the kernel (which matches real paths) will enforce.
    real = tmp_path / "real_ws"
    real.mkdir()
    link = tmp_path / "link_ws"
    link.symlink_to(real)
    policy = _policy(rw=[link])
    assert policy.rw_paths == (real.resolve(),)  # the symlink, not silently trusted, is resolved
    assert link.resolve() not in [link]  # sanity: link != target


def test_policy_dedups_paths(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    policy = _policy(rw=[d, d, tmp_path / "d"])
    assert policy.rw_paths == (d.resolve(),)


# --------------------------------------------------------------------------- #
# macOS SBPL generation (pure string — runs on any OS)
# --------------------------------------------------------------------------- #


def test_mac_profile_shape_and_network_on(tmp_path):
    run = tmp_path / "run"
    ws = tmp_path / "ws"
    gk = tmp_path / "state" / "cairn"
    for p in (run, ws, gk):
        p.mkdir(parents=True)
    profile = _BACKEND.mac_profile(_policy(rw=[run, ws], ro=[gk], tmp=[tmp_path / "tmp"]))
    # Base: import system.sb (dyld/system reads a process needs to START), deny-default, exec/fork.
    assert '(import "system.sb")' in profile
    assert "(deny default)" in profile
    assert "(allow process-exec* process-fork" in profile
    # rw paths get read+write; the ro gatekeys dir gets read-only ONLY (no file-write* on it).
    assert f'(allow file-read* file-write* (subpath "{run.resolve()}"))' in profile
    assert f'(allow file-read* file-write* (subpath "{ws.resolve()}"))' in profile
    assert f'(allow file-read* (subpath "{gk.resolve()}"))' in profile
    assert f'file-write* (subpath "{gk.resolve()}")' not in profile  # gatekeys is READ-only
    # Invariant 1: the fs posture leaves network ON (the CLI's own model API needs it).
    assert "(allow network*)" in profile
    assert "(deny network*)" not in profile


def test_mac_profile_network_off_when_disabled(tmp_path):
    profile = _BACKEND.mac_profile(_policy(rw=[tmp_path], network=False))
    assert "(deny network*)" in profile
    assert "(allow network*)" not in profile


def test_mac_profile_escapes_quotes_in_paths():
    # A path containing a double-quote must be escaped so it cannot break OUT of the SBPL string
    # literal and inject a directive (profile-injection guard).
    weird = Path('/tmp/a"b')
    profile = _BACKEND.mac_profile(_policy(rw=[weird]))
    resolved = str(weird.resolve())  # /tmp → /private/tmp on macOS
    escaped = resolved.replace(chr(34), chr(92) + chr(34))
    assert escaped != resolved  # the quote was actually escaped
    assert f'(subpath "{escaped}")' in profile


# --------------------------------------------------------------------------- #
# Linux bwrap / landrun generation (pure argv — runs on any OS)
# --------------------------------------------------------------------------- #


def test_linux_bwrap_argv_shape_and_network_shared(tmp_path):
    run = tmp_path / "run"
    ws = tmp_path / "ws"
    gk = tmp_path / "state"
    for p in (run, ws, gk):
        p.mkdir()
    argv = _BACKEND.linux_bwrap_argv("/usr/bin/bwrap", ["claude", "-p"], _policy(rw=[run, ws], ro=[gk], tmp=[tmp_path / "t"]))
    assert argv[0] == "/usr/bin/bwrap"
    # Invariant 1: network stays shared — NO --unshare-net / --unshare-all anywhere in the argv.
    assert "--unshare-net" not in argv
    assert "--unshare-all" not in argv
    # Env is NOT cleared (would drop cairn's PATH-with-shim-dir / XDG_STATE_HOME / CAIRN_*).
    assert "--clearenv" not in argv
    # rw binds are read-write; the gatekeys dir is ro-bind (read the secret, cannot forge — W3c).
    assert _adjacent(argv, "--bind", str(run.resolve()))
    assert _adjacent(argv, "--bind", str(ws.resolve()))
    assert _adjacent(argv, "--ro-bind-try", str(gk.resolve()))
    assert not _adjacent(argv, "--bind", str(gk.resolve()))  # gatekeys never read-WRITE bound
    # Contained command is after the -- separator.
    assert argv[-3:] == ["--", "claude", "-p"]
    assert "--chdir" in argv  # cwd set to run_dir inside the namespace


def test_linux_landrun_argv_shape(tmp_path):
    run = tmp_path / "run"
    gk = tmp_path / "state"
    for p in (run, gk):
        p.mkdir()
    argv = _BACKEND.linux_landrun_argv("/usr/bin/landrun", ["claude"], _policy(rw=[run], ro=[gk]))
    assert argv[0] == "/usr/bin/landrun"
    assert _adjacent(argv, "--rwx", str(run.resolve()))
    assert _adjacent(argv, "--ro", str(gk.resolve()))
    assert argv[-2:] == ["--", "claude"]


def _adjacent(argv: list[str], flag: str, value: str) -> bool:
    """Is ``value`` the argument immediately following an occurrence of ``flag``?"""
    return any(argv[i] == flag and argv[i + 1] == value for i in range(len(argv) - 1))


# --------------------------------------------------------------------------- #
# SandboxWrapper — posture gating + degradation (any OS)
# --------------------------------------------------------------------------- #


class _StubBackend(SandboxBackend):
    def __init__(self, available: bool) -> None:
        self._ok = available
        self.wrapped = False

    def available(self) -> bool:
        return self._ok

    def wrap(self, argv, policy):
        if not self._ok:
            raise SandboxUnavailable("stub backend unavailable")
        self.wrapped = True
        return ["STUB", *argv]


def test_off_posture_is_pure_passthrough(tmp_path):
    # codex/grok/shell/stub (posture off) → argv byte-identical, no backend, no warning.
    wrapper = build_wrapper("off")
    assert wrapper.backend is None
    inv = make_inv(tmp_path)
    argv = ["codex", "exec", "--sandbox", "workspace-write"]
    out = wrapper.wrap(list(argv), inv, run_dir=inv.cwd, workspace=inv.cwd)
    assert out == argv  # unchanged
    assert wrapper.take_warning() is None


def test_fs_posture_applies_backend(tmp_path):
    wrapper = SandboxWrapper("fs", _StubBackend(available=True))
    inv = make_inv(tmp_path)
    out = wrapper.wrap(["claude", "-p"], inv, run_dir=inv.cwd, workspace=inv.cwd)
    assert out == ["STUB", "claude", "-p"]
    assert wrapper.take_warning() is None


def test_degradation_when_primitive_unavailable(tmp_path):
    # Invariant 4 / degradation policy: unavailable primitive → argv UNCHANGED + a one-time
    # sandbox-unavailable reason, never a raise into the run.
    wrapper = SandboxWrapper("fs", _StubBackend(available=False))
    inv = make_inv(tmp_path)
    out = wrapper.wrap(["claude", "-p"], inv, run_dir=inv.cwd, workspace=inv.cwd)
    assert out == ["claude", "-p"]  # ran unsandboxed
    reason = wrapper.take_warning()
    assert reason is not None and "no OS filesystem sandbox" in reason
    # Deduped: the same reason is only surfaced once across steps.
    wrapper.wrap(["claude", "-p"], inv, run_dir=inv.cwd, workspace=inv.cwd)
    assert wrapper.take_warning() is None


def test_degradation_on_wrap_raise(tmp_path):
    # A backend that reports available() but raises SandboxUnavailable at wrap time still degrades
    # (returns argv unchanged) rather than failing the run.
    class _RaiseAtWrap(_StubBackend):
        def available(self):
            return True

        def wrap(self, argv, policy):
            raise SandboxUnavailable("compile failed")

    wrapper = SandboxWrapper("fs", _RaiseAtWrap(available=True))
    inv = make_inv(tmp_path)
    out = wrapper.wrap(["claude"], inv, run_dir=inv.cwd, workspace=inv.cwd)
    assert out == ["claude"]
    assert wrapper.take_warning() == "compile failed"


# --------------------------------------------------------------------------- #
# Real containment (skipif no primitive)
# --------------------------------------------------------------------------- #


@requires_sandbox
def test_containment_write_confined_read_denied(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    # Plant an out-of-scope "host secret" under $HOME (never in the policy) to prove read-denial.
    home_secret = Path(os.environ["HOME"]) / ".cairn_c8_test_secret"
    home_secret.write_text("topsecret", encoding="utf-8")
    try:
        policy = _policy(rw=[run])
        # Write INSIDE run_dir → allowed.
        r = _run_wrapped(f"echo ok > {run}/f && cat {run}/f", policy)
        assert r.returncode == 0 and "ok" in r.stdout, r.stderr
        # Write OUTSIDE (host $HOME) → denied by the kernel, step reports it, non-zero.
        r = _run_wrapped(
            f"echo pwned > {home_secret.parent}/.cairn_c8_pwned && echo LEAK || echo CONTAINED",
            policy,
        )
        assert "CONTAINED" in r.stdout and "LEAK" not in r.stdout
        assert not (home_secret.parent / ".cairn_c8_pwned").exists()
        # Read a host secret → denied.
        r = _run_wrapped(f"cat {home_secret} 2>/dev/null && echo LEAK || echo CONTAINED", policy)
        assert "CONTAINED" in r.stdout and "topsecret" not in r.stdout
    finally:
        home_secret.unlink(missing_ok=True)
        (home_secret.parent / ".cairn_c8_pwned").unlink(missing_ok=True)


@requires_sandbox
def test_gatekeys_dir_is_read_only(tmp_path):
    # The W3c close: inside the wrap the agent can READ the gatekeys dir (the hook needs the secret)
    # but CANNOT write/forge a manifest there.
    run = tmp_path / "run"
    run.mkdir()
    gk = tmp_path / "state" / "cairn" / "guard-manifests"
    gk.mkdir(parents=True)
    (gk / "real.json").write_text('{"m":1}', encoding="utf-8")
    policy = _policy(rw=[run], ro=[gk.parent])  # ro-allow the .../cairn state dir
    # READ works.
    r = _run_wrapped(f"cat {gk}/real.json >/dev/null 2>&1 && echo READ-OK || echo READ-DENIED", policy)
    assert "READ-OK" in r.stdout, r.stderr
    # WRITE (forge a manifest) is kernel-denied.
    r = _run_wrapped(f"echo forged > {gk}/forged.json && echo LEAK || echo DENIED", policy)
    assert "DENIED" in r.stdout and "LEAK" not in r.stdout
    assert not (gk / "forged.json").exists()


@requires_sandbox
def test_guard_shim_denies_through_the_wrap(tmp_path, monkeypatch):
    # Invariant 2: the guard chain still ENFORCES inside the sandbox. A shimmed command that a
    # signed manifest denies is still denied (exit 2) when the whole thing runs WRAPPED — proving
    # the shim can exec python, import cairn, and READ its signed manifest from the ro gatekeys dir.
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))  # gatekeys (secret + manifest) land here
    run = tmp_path / "run"
    run.mkdir()
    shim_dir = run / "shims"
    bin_dir = run / "bin"
    ws = run / "ws"
    for p in (bin_dir, ws):
        p.mkdir()
    # A real "brease" the shim would exec if it allowed — records if it (wrongly) runs.
    record = run / "ran.txt"
    real = bin_dir / "brease"
    real.write_text(f'#!/bin/sh\necho "$@" >> "{record}"\nexit 0\n', encoding="utf-8")
    real.chmod(0o755)
    guards = [
        GuardDecl(
            name="no-create", match_tool="bash", match_command="brease* createMedia*",
            check=CHECKS / "deny_all.py", enforce=("shim", "post"), on_error="deny", when=None,
        ),
    ]
    build_shims(guards, shim_dir=shim_dir, workspace_dir=ws, run_dir=run)
    assert guard_manifest_path(run, "shim").exists()  # manifest written to the gatekeys dir

    # Policy: rw run_dir (shims/bin/ws live here), ro the gatekeys state dir + interpreter so the
    # shim's `python -m cairn.kernel.guards --shim-check` can run and read its signed manifest.
    from cairn.kernel.sandbox import _interpreter_ro_paths, _state_cairn_dir

    env = {
        "XDG_STATE_HOME": str(state),
        "PATH": f"{shim_dir}:{bin_dir}:{os.environ.get('PATH','')}",
        "CAIRN_RUN_DIR": str(run),
        "CAIRN_SHIM_DIR": str(shim_dir),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    ro = [_state_cairn_dir(env), *_interpreter_ro_paths()]
    policy = _policy(rw=[run], ro=ro)
    argv = _BACKEND.wrap(["brease", "media", "createMedia", "x.png"], policy)
    r = subprocess.run(argv, env={**os.environ, **env}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2, f"expected deny (2); stdout={r.stdout!r} stderr={r.stderr!r}"
    assert not record.exists()  # the real binary was never reached — denied through the wrap


@requires_sandbox
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec stderr behaviour")
def test_mac_wrap_does_not_pollute_step_parse(tmp_path):
    # The macOS deprecation warning concern: whatever sandbox-exec emits, a STEP sentinel printed by
    # the wrapped process must still parse (the parser scans for <<<STEP + json-raw-decodes, immune
    # to a leading noise line). Also assert this OS does not emit the deprecation line at all.
    from cairn.executors.base import parse_step_sentinel

    run = tmp_path / "run"
    run.mkdir()
    policy = _policy(rw=[run])
    script = "echo preamble; printf '<<<STEP {\"status\":\"done\",\"summary\":\"ok\"} STEP>>>\\n'"
    r = _run_wrapped(script, policy)
    combined = r.stdout + r.stderr
    assert NativeBackend.MAC_DEPRECATION_NOISE not in combined  # not emitted on this macOS
    assert parse_step_sentinel(combined) == {"status": "done", "summary": "ok"}


# --------------------------------------------------------------------------- #
# Executor wiring — invoke() applies the wrap + surfaces degradation
# --------------------------------------------------------------------------- #


class _FakeFsExecutor(CliExecutor):
    """A minimal fs-posture executor whose command is a trivial shell script — needs no real CLI."""

    name = "fake-fs"
    capabilities = Capabilities(
        blocking_hooks=None, output_schema=False, session_capture=None,
        installs_hooks=False, sandbox="fs",
    )

    def _build_command(self, inv, prompt_text):
        return ["/bin/sh", "-c", "echo hi"], None


def _fake_cfg() -> ExecutorConfig:
    return ExecutorConfig(name="fake-fs")


def test_invoke_degrades_loudly_when_unavailable(tmp_path, capsys):
    # When the primitive is stubbed unavailable, invoke() runs the step UNSANDBOXED but prints a
    # single `sandbox-unavailable` line to stderr (loud-not-silent).
    ex = _FakeFsExecutor(_fake_cfg())
    ex._sandbox_wrapper = SandboxWrapper("fs", _StubBackend(available=False))
    inv = make_inv(tmp_path, prompt="x")
    result = ex.invoke(inv)
    assert result.exit_code == 0  # ran anyway
    err = capsys.readouterr().err
    assert "sandbox-unavailable: fake-fs" in err


def test_invoke_off_posture_never_warns(tmp_path, capsys):
    class _OffExec(_FakeFsExecutor):
        name = "fake-off"
        capabilities = Capabilities(
            blocking_hooks=None, output_schema=False, session_capture=None,
            installs_hooks=False, sandbox="off",
        )

    ex = _OffExec(_fake_cfg())
    ex.invoke(make_inv(tmp_path, prompt="x"))
    assert "sandbox-unavailable" not in capsys.readouterr().err


@requires_sandbox
def test_invoke_fs_posture_wraps_and_runs(tmp_path):
    # End-to-end through the real backend: a fs executor's step runs successfully under the wrap,
    # writing its artifact into the run dir (which the sandbox permits).
    class _WriteExec(_FakeFsExecutor):
        def _build_command(self, inv, prompt_text):
            marker = Path(inv.cwd) / "artifact.txt"
            return ["/bin/sh", "-c", f"echo done > {marker}; echo ran"], None

    ex = _WriteExec(_fake_cfg())
    inv = make_inv(tmp_path, prompt="x")
    result = ex.invoke(inv)
    assert result.exit_code == 0
    assert (Path(inv.cwd) / "artifact.txt").read_text().strip() == "done"


# --------------------------------------------------------------------------- #
# doctor — sandbox-availability WARN check
# --------------------------------------------------------------------------- #


def test_doctor_warns_when_fs_posture_unavailable(tmp_path):
    ex = _FakeFsExecutor(_fake_cfg())
    ex._sandbox_wrapper = SandboxWrapper("fs", _StubBackend(available=False))
    findings = ex._sandbox_findings()
    assert len(findings) == 1
    assert findings[0].level == "warning"
    assert "UNSANDBOXED" in findings[0].message


def test_doctor_silent_when_available_or_off(tmp_path):
    # Available primitive → no finding.
    ex = _FakeFsExecutor(_fake_cfg())
    ex._sandbox_wrapper = SandboxWrapper("fs", _StubBackend(available=True))
    assert ex._sandbox_findings() == []
    # Off posture → no backend, no finding.
    off = _FakeFsExecutor(_fake_cfg())
    off._sandbox_wrapper = build_wrapper("off")
    assert off._sandbox_findings() == []
