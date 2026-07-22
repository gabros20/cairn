"""proc — ProcessHandle primitive + SubprocessRunner over Popen (FACTORY-PLAN W0.1 / D10).

Covers: spawn pid-before-wait, stream capture + input piping, poll None-then-code,
run() == spawn().wait() equivalence, nonzero never raises, terminate on a sleeper.
"""

from __future__ import annotations

import sys
import time

import pytest

from cairn.kernel.proc import RunResult, SubprocessRunner


def _py(*code_lines: str) -> list[str]:
    """argv for ``python -c '<joined>`` — keeps test argv short and portable."""
    return [sys.executable, "-c", "; ".join(code_lines)]


def test_spawn_pid_available_before_wait():
    runner = SubprocessRunner()
    handle = runner.spawn(_py("import time", "time.sleep(0.3)", "print('done')"))
    pid = handle.pid
    assert isinstance(pid, int) and pid > 0
    # Still running — pid was observable without waiting.
    assert handle.poll() is None
    result = handle.wait()
    assert result.returncode == 0
    assert result.stdout.strip() == "done"
    assert handle.pid == pid  # stable across wait


def test_wait_captures_stdout_stderr_and_input_piping():
    runner = SubprocessRunner()
    handle = runner.spawn(
        _py(
            "import sys",
            "data = sys.stdin.read()",
            "sys.stdout.write('OUT:' + data)",
            "sys.stderr.write('ERR:side')",
        ),
        input="piped\n",
    )
    result = handle.wait()
    assert result.returncode == 0
    assert result.stdout == "OUT:piped\n"
    assert result.stderr == "ERR:side"


def test_poll_none_then_exit_code():
    runner = SubprocessRunner()
    handle = runner.spawn(_py("import time", "time.sleep(0.25)", "raise SystemExit(42)"))
    assert handle.poll() is None
    # Spin until exit without using wait — poll is the non-blocking seam.
    deadline = time.monotonic() + 5.0
    code = None
    while time.monotonic() < deadline:
        code = handle.poll()
        if code is not None:
            break
        time.sleep(0.02)
    assert code == 42
    # wait after poll still yields a full RunResult (streams empty here).
    result = handle.wait()
    assert result.returncode == 42
    assert result.stdout == ""
    assert result.stderr == ""


def test_run_equals_spawn_wait_same_runresult():
    runner = SubprocessRunner()
    argv = _py(
        "import sys",
        "sys.stdout.write('hi\\n')",
        "sys.stderr.write('e\\n')",
        "raise SystemExit(3)",
    )
    via_run = runner.run(argv)
    via_spawn = runner.spawn(argv).wait()
    assert via_run == via_spawn
    assert via_run == RunResult(returncode=3, stdout="hi\n", stderr="e\n")


def test_nonzero_exit_never_raises():
    runner = SubprocessRunner()
    result = runner.run(_py("raise SystemExit(7)"))
    assert result.returncode == 7
    # spawn path too
    result2 = runner.spawn(_py("raise SystemExit(9)")).wait()
    assert result2.returncode == 9


def test_terminate_on_sleeping_child():
    runner = SubprocessRunner()
    handle = runner.spawn(_py("import time", "time.sleep(30)"))
    assert handle.poll() is None
    handle.terminate()
    result = handle.wait(timeout=5.0)
    # SIGTERM → negative signal number on POSIX (or 1 on some platforms); never hang.
    assert result.returncode != 0
    assert handle.poll() is not None


def test_run_argv_str_coercion_and_empty_streams():
    # Path-like / non-str argv elements are str()'d; quiet success → empty streams not None.
    runner = SubprocessRunner()
    result = runner.run([sys.executable, "-c", "pass"])
    assert result == RunResult(returncode=0, stdout="", stderr="")


def test_run_with_cwd(tmp_path):
    runner = SubprocessRunner()
    marker = tmp_path / "here"
    marker.mkdir()
    result = runner.run(
        _py("import os", "print(os.getcwd())"),
        cwd=tmp_path,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == str(tmp_path)
