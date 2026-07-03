"""base.py additions: STEP sentinel parsing, the shared subprocess runner, ExecTimeout.

This module also defines the shared test helpers (fake-bin PATH, Invocation builder) that
the per-executor test modules import — keeping the fakebin harness in one place.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from cairn.executors import base
from cairn.executors.base import ExecTimeout, parse_step_sentinel, run_process
from cairn.kernel.errors import CairnError
from cairn.kernel.types import Invocation

FIXTURES = Path(__file__).parent / "fixtures"
FAKEBIN = FIXTURES / "fakebin"


# --------------------------------------------------------------------------- #
# Shared helpers (imported by test_executors_{shell,claude,codex,grok}.py)
# --------------------------------------------------------------------------- #


def fake_env(scratch: Path, **extra: str) -> dict[str, str]:
    """A scrubbed-baseline env (PATH incl. fakebin + a canary), plus test knobs."""
    env = {
        "PATH": f"{FAKEBIN}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": os.environ.get("HOME", "/tmp"),
        "CAIRN_CANARY": "canary-value",
        "CAIRN_TEST_SCRATCH": str(scratch),
    }
    env.update(extra)
    return env


def use_fakebin(monkeypatch) -> None:
    """Put the fake CLIs on the *parent* PATH (executable resolution) and plant an
    os.environ-only secret that must never leak into a child's env."""
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("OS_ONLY_SECRET", "leak-me")


def make_inv(
    tmp_path: Path,
    *,
    prompt: str = "do the thing",
    model: str = "opus",
    effort: str | None = "high",
    env: dict[str, str] | None = None,
    timeout_s: int = 30,
    scratch: Path | None = None,
) -> Invocation:
    scratch = scratch or (tmp_path / "scratch")
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text(prompt, encoding="utf-8")
    cwd = tmp_path / "run"
    cwd.mkdir(exist_ok=True)
    return Invocation(
        prompt_file=prompt_file,
        model=model,
        effort=effort,
        cwd=cwd,
        env=env if env is not None else fake_env(scratch),
        timeout_s=timeout_s,
        log_path=tmp_path / "logs" / "step.log",
        return_schema=tmp_path / "return.json",
    )


def read_scratch(scratch: Path) -> tuple[list[str], dict[str, str], str]:
    argv = json.loads((scratch / "argv.json").read_text())
    env = json.loads((scratch / "env.json").read_text())
    stdin = (scratch / "stdin.txt").read_text()
    return argv, env, stdin


# --------------------------------------------------------------------------- #
# parse_step_sentinel
# --------------------------------------------------------------------------- #


def test_parses_a_step_block_into_a_dict():
    text = 'noise\n<<<STEP {"status": "done", "summary": "ok", "artifacts": []} STEP>>>\ntrailing'
    step = parse_step_sentinel(text)
    assert step == {"status": "done", "summary": "ok", "artifacts": []}


def test_absent_sentinel_returns_none():
    assert parse_step_sentinel("just a chatty model saying nothing structured") is None
    assert parse_step_sentinel("") is None


def test_malformed_json_in_block_returns_none():
    assert parse_step_sentinel("<<<STEP {not valid json,} STEP>>>") is None


def test_last_well_formed_block_wins():
    text = (
        '<<<STEP {"status": "blocked", "summary": "first"} STEP>>>\n'
        'more thinking...\n'
        '<<<STEP {"status": "done", "summary": "second"} STEP>>>'
    )
    assert parse_step_sentinel(text) == {"status": "done", "summary": "second"}


def test_non_object_json_returns_none():
    # A JSON array is well-framed but is not a STEP object.
    assert parse_step_sentinel('<<<STEP [1, 2, 3] STEP>>>') is None


def test_last_WELL_FORMED_block_wins_over_a_trailing_broken_block():
    # A good block followed by a broken one: fall back to the last block that parses.
    text = (
        '<<<STEP {"status": "done", "summary": "good"} STEP>>>\n'
        '<<<STEP {broken,,} STEP>>>'
    )
    assert parse_step_sentinel(text) == {"status": "done", "summary": "good"}


# --------------------------------------------------------------------------- #
# run_process
# --------------------------------------------------------------------------- #


def test_run_process_captures_output_and_tees_to_log(tmp_path):
    log = tmp_path / "out.log"
    code, out, dur = run_process(
        ["/bin/sh", "-c", "printf 'hello\\nworld\\n'"],
        stdin_text=None,
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        timeout_s=10,
        log_path=log,
    )
    assert code == 0
    assert "hello" in out and "world" in out
    assert "hello" in log.read_text()  # streamed to disk, not just captured
    assert dur >= 0.0


def test_run_process_passes_env_exactly_never_inheriting_os_environ(tmp_path, monkeypatch):
    monkeypatch.setenv("OS_ONLY_SECRET", "leak-me")
    code, out, _ = run_process(
        ["/bin/sh", "-c", 'echo "canary=$CAIRN_CANARY os=${OS_ONLY_SECRET:-absent}"'],
        stdin_text=None,
        env={"PATH": "/usr/bin:/bin", "CAIRN_CANARY": "present"},
        cwd=tmp_path,
        timeout_s=10,
        log_path=tmp_path / "l.log",
    )
    assert code == 0
    assert "canary=present" in out
    assert "os=absent" in out  # os.environ was NOT merged into the child


def test_run_process_feeds_stdin(tmp_path):
    code, out, _ = run_process(
        ["/bin/sh", "-c", "cat"],
        stdin_text="piped-prompt-body",
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        timeout_s=10,
        log_path=tmp_path / "l.log",
    )
    assert code == 0
    assert "piped-prompt-body" in out


def test_run_process_propagates_exit_code(tmp_path):
    code, _, _ = run_process(
        ["/bin/sh", "-c", "exit 7"],
        stdin_text=None,
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        timeout_s=10,
        log_path=tmp_path / "l.log",
    )
    assert code == 7


def test_run_process_timeout_kills_and_raises(tmp_path):
    start = time.monotonic()
    with pytest.raises(ExecTimeout):
        run_process(
            ["/bin/sh", "-c", "sleep 10"],
            stdin_text=None,
            env={"PATH": "/usr/bin:/bin"},
            cwd=tmp_path,
            timeout_s=0.4,
            log_path=tmp_path / "l.log",
        )
    assert time.monotonic() - start < 5  # returned promptly, did not wait out the sleep


def test_run_process_timeout_reaps_the_child(tmp_path, monkeypatch):
    from cairn.executors import base as basemod

    real_popen = basemod.subprocess.Popen
    created: list = []

    def capture(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        created.append(proc)
        return proc

    monkeypatch.setattr(basemod.subprocess, "Popen", capture)
    with pytest.raises(ExecTimeout):
        run_process(
            ["/bin/sh", "-c", "sleep 10"],
            stdin_text=None,
            env={"PATH": "/usr/bin:/bin"},
            cwd=tmp_path,
            timeout_s=0.4,
            log_path=tmp_path / "l.log",
        )
    proc = created[0]
    assert proc.returncode is not None  # waited on → reaped, not left a zombie


def test_run_process_tolerates_non_utf8_output(tmp_path):
    # A lone 0xFF byte would blow up strict utf-8 decoding and silently kill the pump thread.
    code, out, _ = run_process(
        ["/bin/sh", "-c", "printf '\\377'; printf 'tail\\n'"],
        stdin_text=None,
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        timeout_s=10,
        log_path=tmp_path / "l.log",
    )
    assert code == 0
    assert "�" in out  # 0xFF decoded to the replacement char, not an exception
    assert "tail" in out  # output after the bad byte still captured
    assert "tail" in (tmp_path / "l.log").read_text()  # and still teed to the log


def test_exectimeout_is_a_cairn_error():
    assert issubclass(ExecTimeout, CairnError)


def test_base_reexports_helpers():
    assert base.parse_step_sentinel is parse_step_sentinel
    assert base.run_process is run_process
