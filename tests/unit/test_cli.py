"""CLI frame: --version works; subcommands are registered but not yet implemented."""

from __future__ import annotations

import subprocess
import sys

import pytest

import cairn
from cairn.cli import SUBCOMMANDS, main
from cairn.kernel.types import ExitCode


def test_version_prints_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert cairn.__version__ in out


def test_version_via_console_script():
    result = subprocess.run(
        [sys.executable, "-m", "cairn", "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert cairn.__version__ in result.stdout


def test_all_expected_subcommands_are_registered():
    assert SUBCOMMANDS == [
        "plan", "run", "resume", "gate", "validate", "trail", "ps",
        "doctor", "test", "new", "compose", "batch", "learnings", "gc", "schedule",
    ]


@pytest.mark.parametrize("cmd", SUBCOMMANDS)
def test_known_subcommand_is_not_implemented_yet(cmd, capsys):
    rc = main([cmd])
    assert rc == ExitCode.CONFIG
    err = capsys.readouterr().err
    assert f"not implemented yet: {cmd}" in err


def test_unknown_command_exits_config():
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == ExitCode.CONFIG


def test_no_subcommand_returns_config_and_prints_help(capsys):
    rc = main([])
    assert rc == ExitCode.CONFIG
    assert "usage" in capsys.readouterr().err.lower()
