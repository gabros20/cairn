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

from cairn.kernel.guards import CheckResult, build_shims, matches, run_check
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
    delta = build_shims([g], shim_dir=shim_dir, workspace_dir=ws)

    shim = shim_dir / "brease"  # binary name = first token before glob/space
    assert shim.is_file()
    assert os.access(shim, os.X_OK)
    assert stat.S_IMODE(shim.stat().st_mode) == 0o755

    manifest = json.loads((shim_dir / "manifest.json").read_text())
    assert manifest["workspace_dir"] == str(ws)
    assert manifest["guards"]["no-media"]["match_command"] == "brease* createMedia*"
    assert manifest["guards"]["no-media"]["check"] == str(CHECKS / "allow_all.py")
    assert manifest["guards"]["no-media"]["on_error"] == "deny"

    # env delta puts the shim dir on PATH and names it for runtime exclusion.
    assert delta["CAIRN_SHIM_DIR"] == str(shim_dir)
    assert str(shim_dir) in delta["PATH"]


def test_build_shims_one_shim_per_binary(tmp_path: Path) -> None:
    shim_dir = tmp_path / "shims"
    ws = tmp_path / "ws"
    ws.mkdir()
    guards = [
        guard("allow_all.py", name="a", command="brease* createMedia*"),
        guard("deny_all.py", name="b", command="brease*"),
        guard("allow_all.py", name="c", command="npm run*"),
    ]
    build_shims(guards, shim_dir=shim_dir, workspace_dir=ws)
    shims = sorted(p.name for p in shim_dir.iterdir() if p.name != "manifest.json")
    assert shims == ["brease", "npm"]  # two brease guards collapse to one shim


def test_build_shims_no_guards_is_empty(tmp_path: Path) -> None:
    delta = build_shims([], shim_dir=tmp_path / "shims", workspace_dir=tmp_path)
    assert delta == {}


def test_build_shims_is_idempotent(tmp_path: Path) -> None:
    shim_dir = tmp_path / "shims"
    ws = tmp_path / "ws"
    ws.mkdir()
    g = guard("allow_all.py", name="a", command="brease*")
    first = build_shims([g], shim_dir=shim_dir, workspace_dir=ws)
    body1 = (shim_dir / "brease").read_text()
    second = build_shims([g], shim_dir=shim_dir, workspace_dir=ws)
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
    )
    assert (shim_dir / "brease").exists()
    # Rebuild with a guard on a DIFFERENT binary: the old brease shim must be removed, or a
    # brease command finds no matching guard → silent fail-open passthrough.
    build_shims(
        [guard("allow_all.py", name="b", command="vercel*")],
        shim_dir=shim_dir,
        workspace_dir=ws,
    )
    assert not (shim_dir / "brease").exists()
    assert (shim_dir / "vercel").exists()
    manifest = json.loads((shim_dir / "manifest.json").read_text())
    assert set(manifest["guards"]) == {"b"}


def test_build_shims_leaves_foreign_files_alone(tmp_path: Path) -> None:
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    (shim_dir / "not-a-shim.txt").write_text("hand-placed", encoding="utf-8")
    build_shims(
        [guard("allow_all.py", name="a", command="brease*")],
        shim_dir=shim_dir,
        workspace_dir=tmp_path,
    )
    # only cairn-generated shims are swept; unrelated files survive.
    assert (shim_dir / "not-a-shim.txt").read_text() == "hand-placed"


def test_build_shims_empty_binary_prefix_is_a_config_error(tmp_path: Path) -> None:
    from cairn.kernel.errors import ConfigError

    g = guard("allow_all.py", name="bad", command="*brease")  # leading glob → no prefix
    with pytest.raises(ConfigError):
        build_shims([g], shim_dir=tmp_path / "shims", workspace_dir=tmp_path)


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
    build_shims(guards, shim_dir=shim_dir, workspace_dir=ws)

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
    build_shims(guards, shim_dir=shim_dir, workspace_dir=ws)

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
    build_shims(guards, shim_dir=shim_dir, workspace_dir=ws)

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
