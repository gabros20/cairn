"""W5 dark-lane circuit breaker: lane_circuit parse, consecutive counting, admission pause.

Opt-in: a trigger without lane_circuit is byte-identical to today (existing admission
tests unmodified). After N consecutive FAILED dark runs, admission pauses (exit 0,
diagnostic) until a DONE run resets the count or the operator runs
``cairn trigger reset <name>``.
"""

from __future__ import annotations

import io
import json
import textwrap
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cairn.kernel.errors import ConfigError
from cairn.kernel.proc import RunResult, RunnerBase
from cairn.kernel.queue_drain import run_trigger
from cairn.kernel.queue_ledger import (
    circuit_path,
    is_circuit_open,
    note_circuit_outcome,
    read_circuit,
    reset_circuit,
    write_circuit,
)
from cairn.kernel.trigger_host import load_triggers, list_installed_triggers
from cairn.kernel.types import OutcomeClass, RunOutcome, classify_exit
from fstestkit import _CannedHandle

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


class FakeRunner(RunnerBase):
    def __init__(self, canned=None, *, sequence=None):
        self.calls: list[dict] = []
        self._canned = canned or {}
        # Optional per-call returncodes (consumed in order); falls back to canned.
        self._sequence = list(sequence) if sequence is not None else None
        self._seq_i = 0

    def spawn(self, argv, *, input=None, cwd=None) -> _CannedHandle:
        self.calls.append({"argv": list(argv), "input": input, "cwd": cwd})
        if self._sequence is not None:
            if self._seq_i >= len(self._sequence):
                rc = 0
            else:
                rc = self._sequence[self._seq_i]
                self._seq_i += 1
            return _CannedHandle(RunResult(returncode=rc, stdout="", stderr=""))
        key = tuple(argv[:2])
        result = self._canned.get(key, RunResult(returncode=0, stdout="", stderr=""))
        return _CannedHandle(result)


def _workspace(
    tmp_path: Path,
    triggers_yaml: str,
    *,
    pipelines: tuple[str, ...] = ("handle-reply",),
    with_dark: bool = True,
) -> Path:
    (tmp_path / "triggers.yaml").write_text(textwrap.dedent(triggers_yaml), encoding="utf-8")
    pdir = tmp_path / "pipelines"
    pdir.mkdir(exist_ok=True)
    for name in pipelines:
        if with_dark:
            body = (
                f"pipeline: {name}\n"
                "version: 1\n"
                "lanes:\n"
                "  lit: {}\n"
                "  dark: {}\n"
                "steps: []\n"
            )
        else:
            body = f"pipeline: {name}\nsteps: []\n"
        (pdir / f"{name}.yaml").write_text(body, encoding="utf-8")
    return tmp_path


def _stub_mint(monkeypatch, ws: Path):
    import cairn.kernel.queue_drain as qd
    from cairn.kernel.runctl import Minted

    def fake_preallocate(workspace_dir, trigger, claimed_path, *, now):
        run_dir = qd.run_dir_for_item(ws / "runs", trigger.name, claimed_path.name)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text('{"status":"running"}', encoding="utf-8")
        return Minted(run_dir=run_dir)

    monkeypatch.setattr(qd, "preallocate_run", fake_preallocate)


# --------------------------------------------------------------------------- #
# Parse / validate
# --------------------------------------------------------------------------- #


def test_load_lane_circuit_valid(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          lane: dark
          lane_circuit:
            failures: 3
        """,
    )
    t = load_triggers(ws)["handle-reply"]
    assert t.lane == "dark"
    assert t.lane_circuit_failures == 3


def test_load_lane_circuit_absent_is_none(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          lane: dark
        """,
    )
    t = load_triggers(ws)["handle-reply"]
    assert t.lane_circuit_failures is None


def test_load_lane_circuit_bad_failures_zero(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          lane: dark
          lane_circuit:
            failures: 0
        """,
    )
    with pytest.raises(ConfigError, match=r"lane_circuit\.failures"):
        load_triggers(ws)


def test_load_lane_circuit_bad_failures_negative(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          lane: dark
          lane_circuit:
            failures: -2
        """,
    )
    with pytest.raises(ConfigError, match=r"lane_circuit\.failures"):
        load_triggers(ws)


def test_load_lane_circuit_not_a_mapping(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          lane: dark
          lane_circuit: 3
        """,
    )
    with pytest.raises(ConfigError, match=r"lane_circuit.*mapping"):
        load_triggers(ws)


def test_load_lane_circuit_without_lane_is_error(tmp_path):
    """Non-dark rule: lane_circuit requires a dark lane:."""
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          lane_circuit:
            failures: 2
        """,
    )
    with pytest.raises(ConfigError, match=r"lane_circuit.*requires a dark"):
        load_triggers(ws)


def test_load_lane_circuit_on_lit_is_error(tmp_path):
    """Non-dark rule: lit is the park profile — not a dark lane."""
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          lane: lit
          lane_circuit:
            failures: 2
        """,
    )
    with pytest.raises(ConfigError, match=r"lane_circuit.*dark lane"):
        load_triggers(ws)


def test_load_lane_circuit_missing_failures_key(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          lane: dark
          lane_circuit: {}
        """,
    )
    with pytest.raises(ConfigError, match=r"requires 'failures'"):
        load_triggers(ws)


# --------------------------------------------------------------------------- #
# State file shape + note_circuit_outcome counting
# --------------------------------------------------------------------------- #


def test_circuit_state_default_closed(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    assert read_circuit(watch) == {"consecutive_failures": 0}
    assert not is_circuit_open(watch, 3)


def test_note_failed_dark_increments(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    failed = RunOutcome(outcome=OutcomeClass.FAILED)
    s1 = note_circuit_outcome(watch, failed, is_dark=True, failures_threshold=3)
    assert s1["consecutive_failures"] == 1
    assert "opened_at" not in s1
    s2 = note_circuit_outcome(watch, failed, is_dark=True, failures_threshold=3)
    assert s2["consecutive_failures"] == 2
    s3 = note_circuit_outcome(
        watch, failed, is_dark=True, failures_threshold=3, now_iso="2026-07-14T12:00:00.000Z"
    )
    assert s3["consecutive_failures"] == 3
    assert s3.get("opened_at") == "2026-07-14T12:00:00.000Z"
    assert is_circuit_open(watch, 3)
    # Dot-file beside the ledger; durable JSON line.
    path = circuit_path(watch)
    assert path.name == ".circuit"
    assert path.is_file()
    doc = json.loads(path.read_text(encoding="utf-8").strip())
    assert doc["consecutive_failures"] == 3


def test_note_done_resets(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    failed = RunOutcome(outcome=OutcomeClass.FAILED)
    done = RunOutcome(outcome=OutcomeClass.DONE)
    note_circuit_outcome(watch, failed, is_dark=True, failures_threshold=2)
    note_circuit_outcome(watch, failed, is_dark=True, failures_threshold=2)
    assert is_circuit_open(watch, 2)
    s = note_circuit_outcome(watch, done, is_dark=True, failures_threshold=2)
    assert s == {"consecutive_failures": 0}
    assert not is_circuit_open(watch, 2)


def test_note_waiting_does_not_change(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    failed = RunOutcome(outcome=OutcomeClass.FAILED)
    note_circuit_outcome(watch, failed, is_dark=True, failures_threshold=5)
    waiting = classify_exit(6)  # NEEDS_HUMAN
    s = note_circuit_outcome(watch, waiting, is_dark=True, failures_threshold=5)
    assert s["consecutive_failures"] == 1
    waiting8 = classify_exit(8)
    note_circuit_outcome(watch, waiting8, is_dark=True, failures_threshold=5)
    waiting9 = classify_exit(9)
    note_circuit_outcome(watch, waiting9, is_dark=True, failures_threshold=5)
    assert read_circuit(watch)["consecutive_failures"] == 1


def test_note_failed_not_dark_does_not_increment(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    failed = RunOutcome(outcome=OutcomeClass.FAILED)
    s = note_circuit_outcome(watch, failed, is_dark=False, failures_threshold=1)
    assert s["consecutive_failures"] == 0
    assert not is_circuit_open(watch, 1)


def test_note_done_lit_also_resets(tmp_path):
    """A DONE run closes the breaker even when is_dark=False (lit pass recovery)."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    failed = RunOutcome(outcome=OutcomeClass.FAILED)
    note_circuit_outcome(watch, failed, is_dark=True, failures_threshold=1)
    assert is_circuit_open(watch, 1)
    done = RunOutcome(outcome=OutcomeClass.DONE)
    note_circuit_outcome(watch, done, is_dark=False, failures_threshold=1)
    assert not is_circuit_open(watch, 1)


def test_consecutive_interleaved_fail_done_resets_streak(tmp_path):
    """A success mid-streak resets consecutive count (not cumulative lifetime fails)."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    failed = RunOutcome(outcome=OutcomeClass.FAILED)
    done = RunOutcome(outcome=OutcomeClass.DONE)
    # fail, fail, done, fail → consecutive = 1, not open at threshold 3
    note_circuit_outcome(watch, failed, is_dark=True, failures_threshold=3)
    note_circuit_outcome(watch, failed, is_dark=True, failures_threshold=3)
    note_circuit_outcome(watch, done, is_dark=True, failures_threshold=3)
    note_circuit_outcome(watch, failed, is_dark=True, failures_threshold=3)
    assert read_circuit(watch)["consecutive_failures"] == 1
    assert not is_circuit_open(watch, 3)


def test_reset_circuit_closes(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    write_circuit(watch, consecutive_failures=5, opened_at="2026-07-14T12:00:00.000Z")
    assert is_circuit_open(watch, 3)
    reset_circuit(watch)
    assert read_circuit(watch) == {"consecutive_failures": 0}
    assert not is_circuit_open(watch, 3)


# --------------------------------------------------------------------------- #
# Drain integration: N fails → open → refuse; DONE resets; waiting no count
# --------------------------------------------------------------------------- #


def _dark_circuit_ws(tmp_path: Path, *, failures: int = 2) -> Path:
    return _workspace(
        tmp_path,
        f"""
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          lane: dark
          lane_circuit:
            failures: {failures}
        """,
    )


def test_drain_n_failed_dark_opens_breaker(tmp_path, monkeypatch):
    ws = _dark_circuit_ws(tmp_path, failures=2)
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    (watch_abs / "a.json").write_text("a", encoding="utf-8")
    (watch_abs / "b.json").write_text("b", encoding="utf-8")
    (watch_abs / "c.json").write_text("c", encoding="utf-8")

    # a fail, b fail → open; c must not be claimed
    runner = FakeRunner(sequence=[3, 3, 0])  # GATE_FAILED ×2
    err = io.StringIO()
    code = run_trigger(
        "handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW, err=err
    )
    # any FAILED outcome sets any_failed → exit 1 (breaker is back-pressure on the
    # *next* admit, not a remapping of the failed runs themselves)
    assert code == 1
    assert len(runner.calls) == 2  # a + b only
    assert (watch_abs / "c.json").is_file()  # never claimed
    assert is_circuit_open(watch_abs, 2)
    assert "lane circuit open" in err.getvalue()
    assert "consecutive dark failures" in err.getvalue()
    assert "cairn trigger reset handle-reply" in err.getvalue()


def test_open_breaker_next_drain_claims_nothing_exit_0(tmp_path, monkeypatch):
    ws = _dark_circuit_ws(tmp_path, failures=2)
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    write_circuit(watch_abs, consecutive_failures=2, opened_at="2026-07-14T12:00:00.000Z")
    (watch_abs / "new.json").write_text("should not claim", encoding="utf-8")

    err = io.StringIO()
    runner = FakeRunner({("cairn", "run"): RunResult(0, "", "")})
    code = run_trigger(
        "handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW, err=err
    )
    assert code == 0  # back pressure, not failure
    assert runner.calls == []
    assert (watch_abs / "new.json").is_file()
    assert "lane circuit open for 'handle-reply'" in err.getvalue()


def test_done_run_resets_count_and_admission_resumes(tmp_path, monkeypatch):
    ws = _dark_circuit_ws(tmp_path, failures=2)
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    # One failure already counted; next DONE resets; then another item claims fine.
    write_circuit(watch_abs, consecutive_failures=1)
    (watch_abs / "ok.json").write_text("ok", encoding="utf-8")
    (watch_abs / "next.json").write_text("next", encoding="utf-8")

    runner = FakeRunner(sequence=[0, 0])
    code = run_trigger("handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW)
    assert code == 0
    assert len(runner.calls) == 2
    assert read_circuit(watch_abs)["consecutive_failures"] == 0


def test_waiting_park_does_not_increment_breaker(tmp_path, monkeypatch):
    ws = _dark_circuit_ws(tmp_path, failures=1)
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    (watch_abs / "park.json").write_text("park", encoding="utf-8")
    (watch_abs / "after.json").write_text("after", encoding="utf-8")

    # exit 6 = NEEDS_HUMAN waiting — not a failure for the breaker
    runner = FakeRunner(sequence=[6, 0])
    code = run_trigger("handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW)
    assert code == 0
    assert len(runner.calls) == 2  # both claimed; breaker never opened
    assert read_circuit(watch_abs)["consecutive_failures"] == 0
    assert not is_circuit_open(watch_abs, 1)


def test_operator_reset_resumes_admission(tmp_path, monkeypatch):
    ws = _dark_circuit_ws(tmp_path, failures=2)
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    write_circuit(watch_abs, consecutive_failures=5, opened_at="2026-07-14T12:00:00.000Z")
    (watch_abs / "new.json").write_text("x", encoding="utf-8")

    # Open → refuse
    err = io.StringIO()
    code = run_trigger(
        "handle-reply",
        ws,
        runner=FakeRunner({("cairn", "run"): RunResult(0, "", "")}),
        cairn_bin="cairn",
        now=NOW,
        err=err,
    )
    assert code == 0
    assert "lane circuit open" in err.getvalue()

    # Operator reset
    reset_circuit(watch_abs)
    assert not is_circuit_open(watch_abs, 2)

    # Admission resumes
    runner = FakeRunner({("cairn", "run"): RunResult(0, "", "")})
    code = run_trigger("handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW)
    assert code == 0
    assert len(runner.calls) == 1
    assert (watch_abs / ".done" / "new.json").is_file()


def test_no_lane_circuit_byte_identical_no_state_file(tmp_path, monkeypatch):
    """D7: trigger without lane_circuit never writes .circuit and drains as today."""
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
        """,
        with_dark=False,
    )
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    for name in ("c.json", "a.json", "b.json"):
        (watch_abs / name).write_text(name, encoding="utf-8")

    # Even failed runs must not create a circuit file when no lane_circuit.
    runner = FakeRunner(sequence=[3, 0, 0])
    code = run_trigger("handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW)
    assert code == 1  # one FAILED
    assert len(runner.calls) == 3
    assert not circuit_path(watch_abs).exists()
    assert load_triggers(ws)["handle-reply"].lane_circuit_failures is None


def test_trigger_list_surfaces_circuit_state(tmp_path):
    ws = _dark_circuit_ws(tmp_path, failures=3)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    write_circuit(watch_abs, consecutive_failures=3, opened_at="2026-07-14T12:00:00.000Z")
    launchd_dir = tmp_path / "launchd"

    statuses = list_installed_triggers(
        ws, backend="launchd", runner=FakeRunner(), launchd_dir=launchd_dir
    )
    by_name = {s.name: s for s in statuses}
    s = by_name["handle-reply"]
    assert s.circuit_failures == 3
    assert s.circuit_consecutive == 3
    assert s.circuit_open is True

    reset_circuit(watch_abs)
    s2 = list_installed_triggers(
        ws, backend="launchd", runner=FakeRunner(), launchd_dir=launchd_dir
    )[0]
    assert s2.circuit_open is False
    assert s2.circuit_consecutive == 0
    assert s2.circuit_failures == 3


def test_trigger_list_no_circuit_fields_when_absent(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
        """,
        with_dark=False,
    )
    (ws / "inbox" / "replies").mkdir(parents=True)
    s = list_installed_triggers(
        ws, backend="launchd", runner=FakeRunner(), launchd_dir=tmp_path / "launchd"
    )[0]
    assert s.circuit_failures is None
    assert s.circuit_consecutive == 0
    assert s.circuit_open is False


# --------------------------------------------------------------------------- #
# CLI: cairn trigger reset
# --------------------------------------------------------------------------- #


def test_cli_trigger_reset(tmp_path, monkeypatch, capsys):
    from cairn.cli import main

    ws = _dark_circuit_ws(tmp_path, failures=2)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    write_circuit(watch_abs, consecutive_failures=4, opened_at="2026-07-14T12:00:00.000Z")
    assert is_circuit_open(watch_abs, 2)

    rc = main(["trigger", "reset", "handle-reply", "--workspace", str(ws)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "lane circuit reset" in out
    assert not is_circuit_open(watch_abs, 2)
    assert read_circuit(watch_abs)["consecutive_failures"] == 0


def test_cli_trigger_reset_no_circuit_is_config_error(tmp_path, capsys):
    from cairn.cli import main

    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
        """,
        with_dark=False,
    )
    rc = main(["trigger", "reset", "handle-reply", "--workspace", str(ws)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no lane_circuit" in err


def test_cli_trigger_list_json_includes_circuit(tmp_path, capsys):
    from cairn.cli import main

    ws = _dark_circuit_ws(tmp_path, failures=2)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    write_circuit(watch_abs, consecutive_failures=1)
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()

    rc = main(
        [
            "trigger",
            "list",
            "--workspace",
            str(ws),
            "--backend",
            "launchd",
            "--launchd-dir",
            str(launchd_dir),
            "--json",
        ]
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    row = next(r for r in data if r["name"] == "handle-reply")
    assert row["circuit_failures"] == 2
    assert row["circuit_consecutive"] == 1
    assert row["circuit_open"] is False


# --------------------------------------------------------------------------- #
# Fix wave r1: concurrent RMW lock (I1) + write-blind diagnostic (I2)
# --------------------------------------------------------------------------- #


def test_pooled_dark_failures_count_exactly_no_lost_update(tmp_path, monkeypatch):
    """I1: concurrency>1 — N concurrent dark FAILED runs → consecutive_failures == N.

    Without the circuit_lock around note_circuit_outcome, pool workers race the
    read-modify-write and undercount (breaker opens late). This is the regression
    test for that lost-update / fixed-tmp collision class.
    """
    n = 4
    ws = _workspace(
        tmp_path,
        f"""
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          lane: dark
          concurrency: {n}
          lane_circuit:
            failures: {n}
        """,
    )
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    for i in range(n):
        (watch_abs / f"item-{i}.json").write_text(str(i), encoding="utf-8")

    live = 0
    peak = 0
    lock = threading.Lock()

    class SlowFailRunner(RunnerBase):
        def __init__(self):
            self.calls: list[dict] = []

        def spawn(self, argv, *, input=None, cwd=None):
            with lock:
                self.calls.append({"argv": list(argv)})
                nonlocal live, peak
                live += 1
                peak = max(peak, live)

            class SlowHandle:
                def __init__(self):
                    self._pid = 200 + len(self_outer.calls)

                @property
                def pid(self):
                    return self._pid

                def wait(self, timeout=None):
                    # Overlap retires so concurrent note_circuit_outcome is forced.
                    time.sleep(0.05)
                    with lock:
                        nonlocal live
                        live -= 1
                    return RunResult(returncode=3, stdout="", stderr="")  # FAILED

                def poll(self):
                    return None

                def terminate(self):
                    return None

            self_outer = self
            return SlowHandle()

    runner = SlowFailRunner()
    code = run_trigger("handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW)
    assert code == 1  # FAILED outcomes
    assert peak >= 2  # actually concurrent
    assert len(runner.calls) == n
    # Exact count — no lost updates under the pool.
    assert read_circuit(watch_abs)["consecutive_failures"] == n
    assert is_circuit_open(watch_abs, n)


def test_circuit_write_failure_emits_diagnostic_does_not_crash(tmp_path, monkeypatch):
    """I2: state-write failure stays non-fatal for the drain but MUST signal the operator.

    Silent blindness would let a failing dark lane burn the queue with no breaker.
    """
    ws = _dark_circuit_ws(tmp_path, failures=1)
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    (watch_abs / "boom.json").write_text("x", encoding="utf-8")

    import cairn.kernel.queue_ledger as ql

    def boom_write(*_a, **_k):
        raise OSError("disk full (injected)")

    monkeypatch.setattr(ql, "write_circuit", boom_write)

    err = io.StringIO()
    runner = FakeRunner({("cairn", "run"): RunResult(3, "", "")})
    code = run_trigger(
        "handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW, err=err
    )
    # Drain continues (FAILED still sets exit 1 from the run outcome itself).
    assert code == 1
    assert len(runner.calls) == 1
    diag = err.getvalue()
    assert "could not update breaker state" in diag
    assert "breaker may not open" in diag
    assert str(circuit_path(watch_abs)) in diag or ".circuit" in diag
    # Injected write failure → state never advanced (file absent or still closed).
    assert not is_circuit_open(watch_abs, 1)
