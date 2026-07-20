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
    network: bool = False,
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
        network=network,
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


def test_fully_valid_block_parses_with_learnings_intact():
    text = (
        '<<<STEP {"status": "done", "summary": "shipped", "artifacts": ["out.txt"], '
        '"learnings": [{"note": "watch rate limits", "tag": "capture"}]} STEP>>>'
    )
    obj = parse_step_sentinel(text)
    assert obj["status"] == "done"
    assert obj["artifacts"] == ["out.txt"]
    assert obj["learnings"] == [{"note": "watch rate limits", "tag": "capture"}]


def test_status_outside_enum_returns_none():
    obj = parse_step_sentinel('<<<STEP {"status": "done-ish", "summary": "ok"} STEP>>>')
    assert obj is None


def test_artifacts_not_a_list_of_strings_returns_none():
    obj = parse_step_sentinel(
        '<<<STEP {"status": "done", "summary": "ok", "artifacts": [1, 2]} STEP>>>'
    )
    assert obj is None


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


def test_run_process_eof_before_exit_is_not_a_timeout(tmp_path):
    # The reap race that halted a live run: a step closes stdout/stderr (pump hits EOF
    # immediately) but exits a beat later. Clocking the pipe + a non-blocking poll()
    # misclassified this as "exceeded timeout of 1800s after 0.8s". The wait must be on
    # the process, for the full budget.
    code, _, dur = run_process(
        ["/bin/sh", "-c", "exec 1>&- 2>&-; sleep 0.5; exit 0"],
        stdin_text=None,
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        timeout_s=10,
        log_path=tmp_path / "l.log",
    )
    assert code == 0
    assert dur >= 0.4  # actually waited for the exit, not the EOF


def test_run_process_grandchild_holding_pipe_does_not_stall_or_false_timeout(tmp_path):
    # The inverse misread: the step exits instantly but a backgrounded grandchild inherits
    # the stdout pipe and holds it open. Clocking the pipe burned the whole budget and then
    # raised a false timeout that group-killed the survivor. The step's own exit must win.
    start = time.monotonic()
    code, out, _ = run_process(
        ["/bin/sh", "-c", "sleep 30 & echo started"],
        stdin_text=None,
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        timeout_s=15,
        log_path=tmp_path / "l.log",
    )
    assert code == 0
    assert "started" in out
    assert time.monotonic() - start < 10  # returned on exit + drain grace, not the pipe


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


def test_run_process_redacts_the_log_and_captured_output(tmp_path):
    # A redactor threaded into run_process scrubs each line before it hits disk OR the return
    # value — so a declared secret never lands in logs/<step>.log (SECURITY.md §1.3).
    def redact(line: str) -> str:
        return line.replace("sk-live-DEADBEEF", "∎REDACTED:TOKEN∎")

    log = tmp_path / "out.log"
    code, out, _ = run_process(
        ["/bin/sh", "-c", "printf 'token=sk-live-DEADBEEF\\ndone\\n'"],
        stdin_text=None,
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        timeout_s=10,
        log_path=log,
        redactor=redact,
    )
    assert code == 0
    on_disk = log.read_text()
    assert "sk-live-DEADBEEF" not in on_disk and "∎REDACTED:TOKEN∎" in on_disk
    assert "sk-live-DEADBEEF" not in out and "∎REDACTED:TOKEN∎" in out


def test_run_process_redacts_a_multiline_secret_from_captured_and_log(tmp_path):
    # A secret VALUE containing an embedded newline isn't caught by the per-line pass alone
    # (each streamed line only holds half of it) — the whole-content pass run after the
    # process exits (W6-C, grok-F10) must still catch it, both in the text the walker parses
    # for the STEP block and in the on-disk log (this repo's choice (a): a final rewrite pass
    # over the bounded step log).
    secret = "sk-live-DEAD\nBEEF"

    def redact(text: str) -> str:
        return text.replace(secret, "∎REDACTED:TOKEN∎")

    log = tmp_path / "out.log"
    code, out, _ = run_process(
        ["/bin/sh", "-c", "printf 'token=sk-live-DEAD\\nBEEF-tail\\n'"],
        stdin_text=None,
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        timeout_s=10,
        log_path=log,
        redactor=redact,
    )
    assert code == 0
    assert secret not in out
    assert "∎REDACTED:TOKEN∎" in out
    on_disk = log.read_text()
    assert secret not in on_disk
    assert "∎REDACTED:TOKEN∎" in on_disk


def test_run_process_skips_log_rewrite_when_reader_still_alive(tmp_path):
    # Same shape as test_run_process_grandchild_holding_pipe_does_not_stall_or_false_timeout:
    # a backgrounded grandchild inherits the stdout pipe and holds it open past the drain
    # grace, so the pump thread is still the log's writer when run_process would otherwise
    # rewrite it. The reader-gated rewrite (W6-C) must SKIP in that case (no second writer,
    # no corruption) — proven here by a MULTILINE secret that survives on disk (residual)
    # while still being caught in the RETURNED text (never gated on the reader).
    secret = "SEC\nRET"

    def redact(text: str) -> str:
        return text.replace(secret, "∎REDACTED:TOKEN∎")

    log = tmp_path / "l.log"
    code, out, _ = run_process(
        ["/bin/sh", "-c", "printf 'a=SEC\\nRET-b\\n'; sleep 30 & echo started"],
        stdin_text=None,
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        timeout_s=15,
        log_path=log,
        redactor=redact,
    )
    assert code == 0
    # The whole-content pass on the returned/parsed text always runs — it's an in-memory
    # snapshot, not a second writer, so it never needs the reader-drained gate.
    assert secret not in out
    assert "∎REDACTED:TOKEN∎" in out
    # The on-disk log, by contrast, only gets the per-line pass here (documented residual):
    # the reader was still alive, so the whole-content rewrite was skipped, not raced.
    on_disk = log.read_text()
    assert secret in on_disk
    assert "started" in on_disk  # log is intact, not corrupted or truncated by a race


def test_run_process_timeout_path_rewrites_log_when_reader_drains(tmp_path):
    # On a timeout, the whole process GROUP is SIGKILLed (no surviving backgrounded
    # grandchild in this case) — the pipe closes, the pump drains, and the reader-gated
    # rewrite (W6-C) must still run on this path even though it raises instead of
    # returning: a multiline secret is caught in the log here too.
    secret = "TOK\nEN99"

    def redact(text: str) -> str:
        return text.replace(secret, "∎REDACTED:TOKEN∎")

    log = tmp_path / "l.log"
    with pytest.raises(ExecTimeout):
        run_process(
            ["/bin/sh", "-c", "printf 'x=TOK\\nEN99-y\\n'; sleep 30"],
            stdin_text=None,
            env={"PATH": "/usr/bin:/bin"},
            cwd=tmp_path,
            timeout_s=0.5,
            log_path=log,
            redactor=redact,
        )
    on_disk = log.read_text()
    assert secret not in on_disk
    assert "∎REDACTED:TOKEN∎" in on_disk


def test_run_process_without_redactor_is_byte_identical(tmp_path):
    # No redactor ⇒ the stream is teed verbatim (the default path stays unchanged).
    log = tmp_path / "out.log"
    _code, out, _ = run_process(
        ["/bin/sh", "-c", "printf 'a\\nb\\n'"],
        stdin_text=None,
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        timeout_s=10,
        log_path=log,
    )
    assert out == "a\nb\n"
    assert log.read_text() == "a\nb\n"


def test_exectimeout_is_a_cairn_error():
    assert issubclass(ExecTimeout, CairnError)


def test_base_reexports_helpers():
    assert base.parse_step_sentinel is parse_step_sentinel
    assert base.run_process is run_process
