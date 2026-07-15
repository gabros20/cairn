"""The guards engine — check runner, command matcher, PATH shims (ARCHITECTURE §4).

Behaviour tests against ``cairn.kernel.guards`` through its public surface: real check
scripts on disk (allow / deny-with-reason / crash / hang / secret-canary), GuardDecls
constructed directly, and — for the shim layer — a real fake ``brease`` binary invoked
through a generated shim on a doctored PATH. Each test asserts one observable property:
the check contract (0 allow / 2 deny / anything-else → on_error), the safe env subset,
the glob-with-spaces matcher, and end-to-end shim enforcement with exit-code passthrough.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import io

from cairn.kernel.gatekeys import guard_manifest_path
from cairn.kernel.guards import (
    CheckResult,
    build_shims,
    main,
    matches,
    run_check,
    write_manifest,
)
from cairn.kernel.plan import GuardDecl

CHECKS = Path(__file__).parent / "fixtures" / "guard-checks"


def guard(
    check_name: str,
    *,
    name: str = "g",
    tool: str = "bash",
    command: str = "brease*",
    on_error: str = "deny",
    enforce: tuple[str, ...] = ("shim", "post"),
) -> GuardDecl:
    return GuardDecl(
        name=name,
        match_tool=tool,
        match_command=command,
        check=CHECKS / check_name,
        enforce=enforce,
        on_error=on_error,
        when=None,
    )


# --------------------------------------------------------------------------- #
# run_check — the 0 / 2 / error contract
# --------------------------------------------------------------------------- #


def test_run_check_allows_on_exit_zero(tmp_path: Path) -> None:
    result = run_check(
        guard("allow_all.py"),
        command="brease status",
        env={},
        run_dir=tmp_path,
        workspace_dir=tmp_path,
    )
    assert isinstance(result, CheckResult)
    assert result.allowed is True
    assert result.reason is None


def test_run_check_denies_on_exit_two_with_last_stderr_line(tmp_path: Path) -> None:
    result = run_check(
        guard("deny_all.py"),
        command="brease media createMedia x.png",
        env={},
        run_dir=tmp_path,
        workspace_dir=tmp_path,
    )
    assert result.allowed is False
    # deny_all.py emits two stderr lines; only the last is the reason.
    assert result.reason == "blocked: screenshot may not become media (F18)"


def test_run_check_crash_fails_open_with_warning(tmp_path: Path) -> None:
    result = run_check(
        guard("crash.py", on_error="allow"),
        command="brease status",
        env={},
        run_dir=tmp_path,
        workspace_dir=tmp_path,
    )
    assert result.allowed is True
    assert result.reason is not None
    assert "failing open" in result.reason
    assert result.failed_open is True  # W6-B: the explicit flag, not just the reason string


def test_run_check_allow_and_fail_closed_do_not_set_failed_open(tmp_path: Path) -> None:
    clean = run_check(
        guard("allow_all.py"), command="brease status", env={},
        run_dir=tmp_path, workspace_dir=tmp_path,
    )
    assert clean.allowed is True and clean.failed_open is False

    closed = run_check(
        guard("crash.py", on_error="deny"), command="brease status", env={},
        run_dir=tmp_path, workspace_dir=tmp_path,
    )
    assert closed.allowed is False and closed.failed_open is False


def test_run_check_crash_fails_closed_naming_error(tmp_path: Path) -> None:
    result = run_check(
        guard("crash.py", on_error="deny"),
        command="brease status",
        env={},
        run_dir=tmp_path,
        workspace_dir=tmp_path,
    )
    assert result.allowed is False
    assert result.reason is not None
    assert "exited 1" in result.reason and "failing closed" in result.reason


def test_run_check_timeout_fails_closed(tmp_path: Path) -> None:
    result = run_check(
        guard("sleep.py", on_error="deny"),
        command="brease status",
        env={},
        run_dir=tmp_path,
        workspace_dir=tmp_path,
        timeout_s=1,
    )
    assert result.allowed is False
    assert result.reason is not None
    assert "timed out" in result.reason and "failing closed" in result.reason


def test_run_check_timeout_fails_open(tmp_path: Path) -> None:
    result = run_check(
        guard("sleep.py", on_error="allow"),
        command="brease status",
        env={},
        run_dir=tmp_path,
        workspace_dir=tmp_path,
        timeout_s=1,
    )
    assert result.allowed is True
    assert result.reason is not None and "timed out" in result.reason


def test_run_check_spawn_failure_is_an_error_outcome(tmp_path: Path) -> None:
    # A non-.py check that cannot be exec'd (missing binary) raises at spawn — an ERROR
    # outcome, not a deny. (.py checks are guaranteed to exist by plan-time validation.)
    missing = guard("does_not_exist", on_error="deny")
    result = run_check(
        missing,
        command="brease status",
        env={},
        run_dir=tmp_path,
        workspace_dir=tmp_path,
    )
    assert result.allowed is False
    assert result.reason is not None and "failing closed" in result.reason


def test_run_check_forwards_only_cairn_env_no_secrets(tmp_path: Path) -> None:
    # deny_if_secret.py denies iff a non-CAIRN_* key reaches it. The engine must strip
    # everything but CAIRN_*, so the canary secret never arrives → allowed.
    result = run_check(
        guard("deny_if_secret.py"),
        command="brease status",
        env={"CAIRN_RUN_ID": "abc", "SECRET_TOKEN": "hunter2", "HOME": "/root"},
        run_dir=tmp_path,
        workspace_dir=tmp_path,
    )
    assert result.allowed is True, result.reason
    assert result.reason is None


def test_run_check_check_process_env_excludes_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A secret sits in the PARENT process env. The check subprocess must NOT inherit it:
    # the engine spawns the check with a filtered env, not the ambient os.environ.
    monkeypatch.setenv("SECRET_CANARY", "hunter2")
    monkeypatch.setenv("BREASE_TOKEN", "leak-me")
    result = run_check(
        guard("deny_if_secret_in_process_env.py"),
        command="brease status",
        env={"CAIRN_RUN_ID": "abc"},
        run_dir=tmp_path,
        workspace_dir=tmp_path,
    )
    assert result.allowed is True, result.reason
    assert result.reason is None


# --------------------------------------------------------------------------- #
# matches — tool exact, command glob (with '*' crossing spaces)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "pattern,command,expected",
    [
        # the load-bearing pin: '*' crosses spaces, so a two-token pattern spans args.
        ("brease* createMedia*", "brease media createMedia --file x.png", True),
        ("brease* createMedia*", "brease media createMedia", True),
        ("brease* createMedia*", "brease status", False),  # no createMedia token
        ("brease*", "brease media createMedia --file x.png", True),
        ("brease*", "brease", True),
        ("brease*", "npm run build", False),
        ("", "anything at all", True),  # empty pattern = no command constraint
    ],
)
def test_matches_command_glob(pattern: str, command: str, expected: bool) -> None:
    g = guard("allow_all.py", command=pattern)
    assert matches(g, tool="bash", command=command) is expected


def test_matches_tool_is_exact() -> None:
    g = guard("allow_all.py", tool="bash", command="brease*")
    assert matches(g, tool="bash", command="brease status") is True
    assert matches(g, tool="python", command="brease status") is False


def test_matches_command_is_case_sensitive() -> None:
    g = guard("allow_all.py", command="brease* createMedia*")
    # createmedia != createMedia — the guard must not be dodged by casing.
    assert matches(g, tool="bash", command="brease media createmedia x") is False


# --------------------------------------------------------------------------- #
# build_shims — one executable shim per binary + a manifest
# --------------------------------------------------------------------------- #


def test_build_shims_writes_executable_shim_and_manifest(tmp_path: Path) -> None:
    shim_dir = tmp_path / "shims"
    ws = tmp_path / "ws"
    ws.mkdir()
    g = guard("allow_all.py", name="no-media", command="brease* createMedia*")
    delta = build_shims([g], shim_dir=shim_dir, workspace_dir=ws, run_dir=tmp_path)

    shim = shim_dir / "brease"  # binary name = first token before glob/space
    assert shim.is_file()
    assert os.access(shim, os.X_OK)
    assert stat.S_IMODE(shim.stat().st_mode) == 0o755

    # The manifest lives OUTSIDE the run dir (protected, agent-unwritable) and carries a MAC.
    manifest_path = guard_manifest_path(tmp_path, "shim")
    assert str(shim_dir) not in str(manifest_path)  # not inside the shim/run dir
    manifest = json.loads(manifest_path.read_text())
    assert manifest["workspace_dir"] == str(ws)
    assert manifest["guards"]["no-media"]["match_command"] == "brease* createMedia*"
    assert manifest["guards"]["no-media"]["check"] == str(CHECKS / "allow_all.py")
    assert manifest["guards"]["no-media"]["on_error"] == "deny"
    assert isinstance(manifest["mac"], str) and manifest["mac"]  # authenticated
    assert manifest["guards"]["no-media"]["check_sha256"]  # check script pinned

    # env delta puts the shim dir on PATH, names it for runtime exclusion, and points at the manifest.
    assert delta["CAIRN_SHIM_DIR"] == str(shim_dir)
    assert str(shim_dir) in delta["PATH"]
    assert delta["CAIRN_SHIM_MANIFEST"] == str(manifest_path)


def test_build_shims_one_shim_per_binary(tmp_path: Path) -> None:
    shim_dir = tmp_path / "shims"
    ws = tmp_path / "ws"
    ws.mkdir()
    guards = [
        guard("allow_all.py", name="a", command="brease* createMedia*"),
        guard("deny_all.py", name="b", command="brease*"),
        guard("allow_all.py", name="c", command="npm run*"),
    ]
    build_shims(guards, shim_dir=shim_dir, workspace_dir=ws, run_dir=tmp_path)
    shims = sorted(p.name for p in shim_dir.iterdir())
    assert shims == ["brease", "npm"]  # two brease guards collapse to one shim; no manifest here


def test_build_shims_no_guards_is_empty(tmp_path: Path) -> None:
    delta = build_shims([], shim_dir=tmp_path / "shims", workspace_dir=tmp_path, run_dir=tmp_path)
    assert delta == {}


def test_build_shims_is_idempotent(tmp_path: Path) -> None:
    shim_dir = tmp_path / "shims"
    ws = tmp_path / "ws"
    ws.mkdir()
    g = guard("allow_all.py", name="a", command="brease*")
    first = build_shims([g], shim_dir=shim_dir, workspace_dir=ws, run_dir=tmp_path)
    body1 = (shim_dir / "brease").read_text()
    second = build_shims([g], shim_dir=shim_dir, workspace_dir=ws, run_dir=tmp_path)
    body2 = (shim_dir / "brease").read_text()
    assert first == second
    assert body1 == body2


def test_build_shims_clears_stale_shims(tmp_path: Path) -> None:
    shim_dir = tmp_path / "shims"
    ws = tmp_path / "ws"
    ws.mkdir()
    build_shims(
        [guard("allow_all.py", name="a", command="brease*")],
        shim_dir=shim_dir,
        workspace_dir=ws,
        run_dir=tmp_path,
    )
    assert (shim_dir / "brease").exists()
    # Rebuild with a guard on a DIFFERENT binary: the old brease shim must be removed, or a
    # brease command finds no matching guard → silent fail-open passthrough.
    build_shims(
        [guard("allow_all.py", name="b", command="vercel*")],
        shim_dir=shim_dir,
        workspace_dir=ws,
        run_dir=tmp_path,
    )
    assert not (shim_dir / "brease").exists()
    assert (shim_dir / "vercel").exists()
    manifest = json.loads(guard_manifest_path(tmp_path, "shim").read_text())
    assert set(manifest["guards"]) == {"b"}


def test_build_shims_leaves_foreign_files_alone(tmp_path: Path) -> None:
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    (shim_dir / "not-a-shim.txt").write_text("hand-placed", encoding="utf-8")
    build_shims(
        [guard("allow_all.py", name="a", command="brease*")],
        shim_dir=shim_dir,
        workspace_dir=tmp_path,
        run_dir=tmp_path,
    )
    # only cairn-generated shims are swept; unrelated files survive.
    assert (shim_dir / "not-a-shim.txt").read_text() == "hand-placed"


def test_build_shims_empty_binary_prefix_is_a_config_error(tmp_path: Path) -> None:
    from cairn.kernel.errors import ConfigError

    g = guard("allow_all.py", name="bad", command="*brease")  # leading glob → no prefix
    with pytest.raises(ConfigError):
        build_shims([g], shim_dir=tmp_path / "shims", workspace_dir=tmp_path, run_dir=tmp_path)


# --------------------------------------------------------------------------- #
# End-to-end: a real fake binary behind a real shim on a doctored PATH.
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_brease(tmp_path: Path) -> tuple[Path, Path]:
    """A fake real ``brease`` that records its argv and exits 7 (to prove passthrough)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "brease-invoked.txt"
    real = bin_dir / "brease"
    real.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{record}"\n'
        "exit 7\n",
        encoding="utf-8",
    )
    real.chmod(0o755)
    return bin_dir, record


def _run_shimmed(
    argv: list[str], *, shim_dir: Path, bin_dir: Path, run_dir: Path
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{shim_dir}:{bin_dir}"
    env["CAIRN_RUN_DIR"] = str(run_dir)
    env["CAIRN_SHIM_DIR"] = str(shim_dir)
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)


def test_shim_denies_and_never_reaches_real_binary(
    tmp_path: Path, fake_brease: tuple[Path, Path]
) -> None:
    bin_dir, record = fake_brease
    shim_dir = tmp_path / "shims"
    ws = tmp_path / "ws"
    ws.mkdir()
    guards = [
        guard("deny_all.py", name="no-media", command="brease* createMedia*"),
        guard("allow_all.py", name="otherwise", command="brease*"),
    ]
    build_shims(guards, shim_dir=shim_dir, workspace_dir=ws, run_dir=tmp_path)

    result = _run_shimmed(
        ["brease", "media", "createMedia", "x.png"],
        shim_dir=shim_dir,
        bin_dir=bin_dir,
        run_dir=tmp_path,
    )
    assert result.returncode == 2, result.stderr
    assert "screenshot may not become media" in result.stderr
    assert not record.exists()  # real binary never ran


def test_shim_allows_and_passes_through_real_exit_code(
    tmp_path: Path, fake_brease: tuple[Path, Path]
) -> None:
    bin_dir, record = fake_brease
    shim_dir = tmp_path / "shims"
    ws = tmp_path / "ws"
    ws.mkdir()
    guards = [
        guard("deny_all.py", name="no-media", command="brease* createMedia*"),
        guard("allow_all.py", name="otherwise", command="brease*"),
    ]
    build_shims(guards, shim_dir=shim_dir, workspace_dir=ws, run_dir=tmp_path)

    result = _run_shimmed(
        ["brease", "status"], shim_dir=shim_dir, bin_dir=bin_dir, run_dir=tmp_path
    )
    assert result.returncode == 7  # real binary's exit code, passed straight through
    assert record.read_text().strip() == "status"


def test_shim_chain_first_allows_second_denies(
    tmp_path: Path, fake_brease: tuple[Path, Path]
) -> None:
    bin_dir, record = fake_brease
    shim_dir = tmp_path / "shims"
    ws = tmp_path / "ws"
    ws.mkdir()
    # Both guards match the SAME command; the first allows, the second denies → denied.
    guards = [
        guard("allow_all.py", name="first", command="brease*"),
        guard("deny_all.py", name="second", command="brease*"),
    ]
    build_shims(guards, shim_dir=shim_dir, workspace_dir=ws, run_dir=tmp_path)

    result = _run_shimmed(
        ["brease", "status"], shim_dir=shim_dir, bin_dir=bin_dir, run_dir=tmp_path
    )
    assert result.returncode == 2, result.stderr
    assert not record.exists()


def test_shim_no_recursion_when_shim_dir_appears_twice_on_path(
    tmp_path: Path, fake_brease: tuple[Path, Path]
) -> None:
    bin_dir, record = fake_brease
    shim_dir = tmp_path / "shims"
    ws = tmp_path / "ws"
    ws.mkdir()
    build_shims(
        [guard("allow_all.py", name="ok", command="brease*")],
        shim_dir=shim_dir,
        workspace_dir=ws,
        run_dir=tmp_path,
    )
    env = dict(os.environ)
    # shim dir spelled two ways (plain + trailing slash): a naive string exclusion would let
    # the second spelling re-select the shim itself → infinite recursion.
    env["PATH"] = f"{shim_dir}:{shim_dir}/:{bin_dir}"
    env["CAIRN_RUN_DIR"] = str(tmp_path)
    env["CAIRN_SHIM_DIR"] = str(shim_dir)
    # A short timeout turns any recursion into a hard failure instead of a hang.
    result = subprocess.run(
        ["brease", "status"], env=env, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 7, result.stderr  # resolved to the real binary, no recursion
    assert record.read_text().strip() == "status"


# --------------------------------------------------------------------------- #
# The --hook-check entry: stdin PreToolUse event → deny-JSON on stdout / silent allow.
# --------------------------------------------------------------------------- #


def _write_hook_manifest(path: Path, guards, run_dir: Path) -> None:
    """Write a SIGNED manifest (per-run MAC + check hashes) at ``path``, keyed to ``run_dir``."""
    write_manifest(guards, workspace_dir=Path(run_dir), run_dir=Path(run_dir), path=Path(path))


def _run_hook_check(
    names, event, *, manifest_path, run_dir, monkeypatch, capsys
) -> tuple[int, str]:
    """Drive ``main(["--hook-check", …])`` in-process: patch stdin to the PreToolUse event JSON,
    point CAIRN_HOOK_MANIFEST/CAIRN_RUN_DIR, capture stdout."""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event) if event is not None else "x"))
    if manifest_path is None:
        monkeypatch.delenv("CAIRN_HOOK_MANIFEST", raising=False)
    else:
        monkeypatch.setenv("CAIRN_HOOK_MANIFEST", str(manifest_path))
    monkeypatch.setenv("CAIRN_RUN_DIR", str(run_dir))
    code = main(["--hook-check", *names])
    return code, capsys.readouterr().out


def _bash_event(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def test_hook_check_denies_with_deny_json_on_stdout(tmp_path, monkeypatch, capsys) -> None:
    manifest = tmp_path / ".cairn" / "hook-manifest.json"
    _write_hook_manifest(
        manifest, [guard("deny_all.py", name="no-media", command="brease*")], tmp_path
    )
    code, out = _run_hook_check(
        ["no-media"], _bash_event("brease media createMedia x.png"),
        manifest_path=manifest, run_dir=tmp_path, monkeypatch=monkeypatch, capsys=capsys,
    )
    assert code == 0  # the deny is carried by the stdout JSON, not the exit code
    payload = json.loads(out)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "screenshot may not become media" in (
        payload["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_hook_check_allows_silently(tmp_path, monkeypatch, capsys) -> None:
    manifest = tmp_path / ".cairn" / "hook-manifest.json"
    _write_hook_manifest(
        manifest, [guard("allow_all.py", name="ok", command="brease*")], tmp_path
    )
    code, out = _run_hook_check(
        ["ok"], _bash_event("brease status"),
        manifest_path=manifest, run_dir=tmp_path, monkeypatch=monkeypatch, capsys=capsys,
    )
    assert code == 0
    assert out == ""  # allow → no output at all


def test_hook_check_allows_when_glob_does_not_match(tmp_path, monkeypatch, capsys) -> None:
    manifest = tmp_path / ".cairn" / "hook-manifest.json"
    # deny_all would deny IF it matched — but the command isn't a brease* command, so the guard
    # simply doesn't apply → allow.
    _write_hook_manifest(
        manifest, [guard("deny_all.py", name="no-media", command="brease*")], tmp_path
    )
    code, out = _run_hook_check(
        ["no-media"], _bash_event("ls -la"),
        manifest_path=manifest, run_dir=tmp_path, monkeypatch=monkeypatch, capsys=capsys,
    )
    assert code == 0
    assert out == ""


def test_hook_check_allows_when_no_command_in_event(tmp_path, monkeypatch, capsys) -> None:
    manifest = tmp_path / ".cairn" / "hook-manifest.json"
    _write_hook_manifest(
        manifest, [guard("deny_all.py", name="no-media", command="brease*")], tmp_path
    )
    # A non-command tool (Read) carries no command string → no command guard can apply → allow.
    code, out = _run_hook_check(
        ["no-media"], {"tool_name": "Read", "tool_input": {"file_path": "x"}},
        manifest_path=manifest, run_dir=tmp_path, monkeypatch=monkeypatch, capsys=capsys,
    )
    assert code == 0
    assert out == ""


def test_hook_check_fails_closed_on_missing_manifest(tmp_path, monkeypatch, capsys) -> None:
    # Manifest path unset AND the run-dir fallback has no manifest → cannot decide → BLOCK.
    code, out = _run_hook_check(
        ["no-media"], _bash_event("brease media createMedia x.png"),
        manifest_path=None, run_dir=tmp_path, monkeypatch=monkeypatch, capsys=capsys,
    )
    assert code == 0  # fail-closed = deny-JSON on stdout + exit 0 (the proven-blocking form, M2)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_check_fails_closed_on_malformed_stdin(tmp_path, monkeypatch, capsys) -> None:
    manifest = tmp_path / ".cairn" / "hook-manifest.json"
    _write_hook_manifest(
        manifest, [guard("deny_all.py", name="no-media", command="brease*")], tmp_path
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("this is not json"))
    monkeypatch.setenv("CAIRN_HOOK_MANIFEST", str(manifest))
    monkeypatch.setenv("CAIRN_RUN_DIR", str(tmp_path))
    code = main(["--hook-check", "no-media"])
    out = capsys.readouterr().out
    assert code == 0  # fail-closed = deny-JSON on stdout + exit 0 (M2)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_and_shim_agree_on_a_matched_deny(tmp_path, monkeypatch, capsys) -> None:
    """The shared chain (_run_chain) means both entries reach the SAME verdict on the same
    guard+command: the shim exits 2 (deny), the hook prints deny-JSON."""
    g = guard("deny_all.py", name="no-media", command="brease*")
    command = "brease media createMedia x.png"

    # --hook-check
    hook_manifest = tmp_path / ".cairn" / "hook-manifest.json"
    _write_hook_manifest(hook_manifest, [g], tmp_path)
    hook_code, hook_out = _run_hook_check(
        ["no-media"], _bash_event(command),
        manifest_path=hook_manifest, run_dir=tmp_path, monkeypatch=monkeypatch, capsys=capsys,
    )
    assert hook_code == 0
    assert json.loads(hook_out)["hookSpecificOutput"]["permissionDecision"] == "deny"

    # --shim-check on the SAME guard+command
    shim_manifest = tmp_path / "shims" / "manifest.json"
    _write_hook_manifest(shim_manifest, [g], tmp_path)
    monkeypatch.setenv("CAIRN_SHIM_MANIFEST", str(shim_manifest))
    monkeypatch.setenv("CAIRN_SHIM_COMMAND", command)
    monkeypatch.setenv("CAIRN_SHIM_TOOL", "bash")
    monkeypatch.setenv("CAIRN_RUN_DIR", str(tmp_path))
    assert main(["--shim-check", "no-media"]) == 2  # shim's deny contract, unchanged


# --------------------------------------------------------------------------- #
# Fail-open visibility (W6-B, codex-F18) — purely additive: a stderr warning on the
# fail-open-allow case, never a changed allow/deny outcome.
# --------------------------------------------------------------------------- #


def test_shim_check_fail_open_still_allows_and_warns_on_stderr(tmp_path, monkeypatch, capsys) -> None:
    manifest = tmp_path / "shims" / "manifest.json"
    _write_hook_manifest(
        manifest, [guard("crash.py", name="flaky", command="brease*", on_error="allow")], tmp_path
    )
    code = _run_shim_check(
        ["flaky"], manifest_path=manifest, command="brease status",
        run_dir=tmp_path, monkeypatch=monkeypatch,
    )
    assert code == 0  # allow/deny outcome unchanged — on_error: allow still allows
    assert "failing open" in capsys.readouterr().err


def test_shim_check_clean_allow_emits_no_fail_open_warning(tmp_path, monkeypatch, capsys) -> None:
    manifest = tmp_path / "shims" / "manifest.json"
    _write_hook_manifest(manifest, [guard("allow_all.py", name="ok", command="brease*")], tmp_path)
    code = _run_shim_check(
        ["ok"], manifest_path=manifest, command="brease status",
        run_dir=tmp_path, monkeypatch=monkeypatch,
    )
    assert code == 0
    assert capsys.readouterr().err == ""


def test_shim_check_fail_closed_still_denies(tmp_path, monkeypatch, capsys) -> None:
    manifest = tmp_path / "shims" / "manifest.json"
    _write_hook_manifest(
        manifest, [guard("crash.py", name="flaky", command="brease*", on_error="deny")], tmp_path
    )
    code = _run_shim_check(
        ["flaky"], manifest_path=manifest, command="brease status",
        run_dir=tmp_path, monkeypatch=monkeypatch,
    )
    assert code == 2  # allow/deny outcome unchanged — on_error: deny still denies


def test_hook_check_fail_open_still_allows_and_warns_on_stderr(tmp_path, monkeypatch, capsys) -> None:
    manifest = tmp_path / ".cairn" / "hook-manifest.json"
    _write_hook_manifest(
        manifest, [guard("crash.py", name="flaky", command="brease*", on_error="allow")], tmp_path
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_bash_event("brease status"))))
    monkeypatch.setenv("CAIRN_HOOK_MANIFEST", str(manifest))
    monkeypatch.setenv("CAIRN_RUN_DIR", str(tmp_path))
    code = main(["--hook-check", "flaky"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""  # allowed → no deny-JSON on stdout, outcome unchanged
    assert "failing open" in captured.err  # but the fail-open is now visible


def test_hook_check_clean_allow_emits_no_fail_open_warning(tmp_path, monkeypatch, capsys) -> None:
    manifest = tmp_path / ".cairn" / "hook-manifest.json"
    _write_hook_manifest(manifest, [guard("allow_all.py", name="ok", command="brease*")], tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_bash_event("brease status"))))
    monkeypatch.setenv("CAIRN_HOOK_MANIFEST", str(manifest))
    monkeypatch.setenv("CAIRN_RUN_DIR", str(tmp_path))
    code = main(["--hook-check", "ok"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "" and captured.err == ""


def test_hook_check_fail_closed_still_denies_with_deny_json(tmp_path, monkeypatch, capsys) -> None:
    manifest = tmp_path / ".cairn" / "hook-manifest.json"
    _write_hook_manifest(
        manifest, [guard("crash.py", name="flaky", command="brease*", on_error="deny")], tmp_path
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_bash_event("brease status"))))
    monkeypatch.setenv("CAIRN_HOOK_MANIFEST", str(manifest))
    monkeypatch.setenv("CAIRN_RUN_DIR", str(tmp_path))
    code = main(["--hook-check", "flaky"])
    captured = capsys.readouterr()
    assert code == 0 and _hook_denied(captured.out)  # allow/deny outcome unchanged
    # Symmetry with the fail-open warning test: a fail-CLOSED deny is carried entirely by
    # the deny-JSON on stdout — the reason is already in the JSON, so no stderr line is
    # needed (and none is emitted; only the fail-open-ALLOW case gets the extra warning).
    assert captured.err == ""


# --------------------------------------------------------------------------- #
# Manifest authentication + tamper detection (C1/C2) — both entries FAIL CLOSED.
# --------------------------------------------------------------------------- #


def _run_shim_check(names, *, manifest_path, command, run_dir, monkeypatch) -> int:
    monkeypatch.setenv("CAIRN_SHIM_MANIFEST", str(manifest_path))
    monkeypatch.setenv("CAIRN_SHIM_COMMAND", command)
    monkeypatch.setenv("CAIRN_SHIM_TOOL", "bash")
    monkeypatch.setenv("CAIRN_RUN_DIR", str(run_dir))
    return main(["--shim-check", *names])


def _hook_denied(out: str) -> bool:
    try:
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    except (ValueError, KeyError, TypeError):
        return False


def _assert_both_fail_closed(manifest_path, tmp_path, monkeypatch, capsys) -> None:
    """The command an untampered `no-media` guard would ALLOW (deny_all only matches createMedia).
    If the manifest is trustworthy the shim allows (exit 0) and the hook is silent; so a DENY here
    proves the tampered/unauthenticated manifest was rejected — fail closed, not fail open."""
    command = "brease status"
    shim_code = _run_shim_check(
        ["no-media"], manifest_path=manifest_path, command=command,
        run_dir=tmp_path, monkeypatch=monkeypatch,
    )
    assert shim_code == 2  # shim fails closed (deny) on an unverifiable manifest
    hook_code, hook_out = _run_hook_check(
        ["no-media"], _bash_event(command),
        manifest_path=manifest_path, run_dir=tmp_path, monkeypatch=monkeypatch, capsys=capsys,
    )
    assert hook_code == 0 and _hook_denied(hook_out)  # hook fails closed (deny-JSON) too


def test_manifest_tamper_empty_guards_fails_closed(tmp_path, monkeypatch, capsys) -> None:
    manifest = tmp_path / "hook.json"
    _write_hook_manifest(manifest, [guard("deny_all.py", name="no-media", command="brease*")], tmp_path)
    # Attacker rewrites the guards to {} but cannot re-sign (no secret) → MAC over content mismatches.
    doc = json.loads(manifest.read_text())
    doc["guards"] = {}
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    _assert_both_fail_closed(manifest, tmp_path, monkeypatch, capsys)


def test_manifest_tamper_flipped_guard_fails_closed(tmp_path, monkeypatch, capsys) -> None:
    manifest = tmp_path / "hook.json"
    _write_hook_manifest(manifest, [guard("deny_all.py", name="no-media", command="brease*")], tmp_path)
    # Flip the check to an allow-all script; the stale MAC no longer matches the new content.
    doc = json.loads(manifest.read_text())
    doc["guards"]["no-media"]["check"] = str(CHECKS / "allow_all.py")
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    _assert_both_fail_closed(manifest, tmp_path, monkeypatch, capsys)


def test_manifest_non_dict_fails_closed(tmp_path, monkeypatch, capsys) -> None:
    # Valid JSON but the wrong top-level type — must NOT raise an uncaught AttributeError (spec
    # Finding 1); the verified loader rejects it and both entries fail closed.
    manifest = tmp_path / "hook.json"
    manifest.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    _assert_both_fail_closed(manifest, tmp_path, monkeypatch, capsys)


def test_manifest_missing_secret_fails_closed(tmp_path, monkeypatch, capsys) -> None:
    from cairn.kernel.gatekeys import gate_keys_dir

    manifest = tmp_path / "hook.json"
    _write_hook_manifest(manifest, [guard("deny_all.py", name="no-media", command="brease*")], tmp_path)
    for key in gate_keys_dir().glob("*.key"):  # delete the per-run secret → cannot authenticate
        key.unlink()
    _assert_both_fail_closed(manifest, tmp_path, monkeypatch, capsys)


def test_check_script_tamper_fails_closed(tmp_path, monkeypatch, capsys) -> None:
    # A check script the agent CAN write: sign the manifest over its original bytes, then rewrite
    # it to exit 0. The signed check_sha256 no longer matches → deny, at both layers. Uses a
    # command the guard MATCHES (brease* createMedia*) so the check would actually run.
    check = tmp_path / "policy_check.py"
    check.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")  # original: denies
    g = GuardDecl(
        name="no-media", match_tool="bash", match_command="brease*",
        check=check, enforce=("hook", "shim"), on_error="deny", when=None,
    )
    manifest = tmp_path / "hook.json"
    _write_hook_manifest(manifest, [g], tmp_path)
    check.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")  # swapped to allow-all

    command = "brease media createMedia x.png"
    assert _run_shim_check(
        ["no-media"], manifest_path=manifest, command=command,
        run_dir=tmp_path, monkeypatch=monkeypatch,
    ) == 2  # shim: integrity mismatch → deny
    hook_code, hook_out = _run_hook_check(
        ["no-media"], _bash_event(command),
        manifest_path=manifest, run_dir=tmp_path, monkeypatch=monkeypatch, capsys=capsys,
    )
    assert hook_code == 0 and _hook_denied(hook_out)
    assert "integrity" in json.loads(hook_out)["hookSpecificOutput"]["permissionDecisionReason"]


def test_authentic_manifest_still_allows_and_denies(tmp_path, monkeypatch, capsys) -> None:
    # Happy path: a legit signed manifest with untampered checks enforces exactly as before.
    manifest = tmp_path / "hook.json"
    _write_hook_manifest(
        manifest,
        [guard("deny_all.py", name="no-media", command="brease* createMedia*")],
        tmp_path,
    )
    # allowed command (glob doesn't match) → silent allow
    code, out = _run_hook_check(
        ["no-media"], _bash_event("brease status"),
        manifest_path=manifest, run_dir=tmp_path, monkeypatch=monkeypatch, capsys=capsys,
    )
    assert code == 0 and out == ""
    # matched command → deny
    code, out = _run_hook_check(
        ["no-media"], _bash_event("brease media createMedia x.png"),
        manifest_path=manifest, run_dir=tmp_path, monkeypatch=monkeypatch, capsys=capsys,
    )
    assert code == 0 and _hook_denied(out)
