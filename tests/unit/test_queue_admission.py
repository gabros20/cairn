"""W3 admission: bounded lazy admission, concurrency pool, priority aging (T11 / D6).

Caps, order:aged, concurrency>1. concurrency:1 + no caps stays on the serial path
(existing drain tests still green; equivalence asserted here explicitly).
"""

from __future__ import annotations

import io
import os
import textwrap
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cairn.kernel.errors import ConfigError
from cairn.kernel.proc import RunResult, RunnerBase
from cairn.kernel.queue_drain import (
    AGE_STEP_SECONDS,
    effective_prio,
    order_candidates,
    run_trigger,
)
from cairn.kernel.queue_ledger import (
    count_by_class,
    pointer_path,
    write_pointer,
)
from cairn.kernel.trigger_host import Trigger, load_triggers, list_installed_triggers
from cairn.kernel.triggerkit import scan_candidates

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


class _CannedHandle:
    def __init__(self, result: RunResult, pid: int = 1):
        self._result = result
        self._pid = pid

    @property
    def pid(self) -> int:
        return self._pid

    def wait(self, timeout=None) -> RunResult:
        return self._result

    def poll(self) -> int | None:
        return self._result.returncode

    def terminate(self) -> None:
        return None


class FakeRunner(RunnerBase):
    def __init__(self, canned=None):
        self.calls: list[dict] = []
        self._canned = canned or {}
        self._lock = threading.Lock()

    def spawn(self, argv, *, input=None, cwd=None) -> _CannedHandle:
        with self._lock:
            self.calls.append({"argv": list(argv), "input": input, "cwd": cwd})
        key = tuple(argv[:2])
        result = self._canned.get(key, RunResult(returncode=0, stdout="", stderr=""))
        return _CannedHandle(result)


def _workspace(tmp_path: Path, triggers_yaml: str, *, pipelines=("handle-reply",)) -> Path:
    (tmp_path / "triggers.yaml").write_text(textwrap.dedent(triggers_yaml), encoding="utf-8")
    pdir = tmp_path / "pipelines"
    pdir.mkdir(exist_ok=True)
    for name in pipelines:
        (pdir / f"{name}.yaml").write_text("pipeline: x\nsteps: []\n", encoding="utf-8")
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


def _park_waiting(
    watch_abs: Path,
    name: str,
    *,
    exit_code: int,
    run_dir: Path | None = None,
) -> None:
    """Seed a parked item in .waiting/ with a classed pointer (D8)."""
    waiting = watch_abs / ".waiting"
    waiting.mkdir(parents=True, exist_ok=True)
    (waiting / name).write_text("parked", encoding="utf-8")
    rd = run_dir if run_dir is not None else watch_abs / "runs" / f"seed-{name}"
    rd.mkdir(parents=True, exist_ok=True)
    write_pointer(
        pointer_path(waiting, name),
        run_dir=rd,
        outcome="waiting",
        exit_code=exit_code,
        child_pid=None,
    )


# --------------------------------------------------------------------------- #
# Trigger key parse / defaults
# --------------------------------------------------------------------------- #


def test_load_trigger_w3_keys_and_defaults(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          concurrency: 4
          order: aged
          waiting_max: 5
          capacity_max: 3
          wip_max: 20
          inbox_max: 50
        """,
    )
    t = load_triggers(ws)["handle-reply"]
    assert t.concurrency == 4
    assert t.order == "aged"
    assert t.waiting_max == 5
    assert t.blocked_max == 5  # default = waiting_max
    assert t.capacity_max == 3
    assert t.wip_max == 20
    assert t.inbox_max == 50


def test_load_trigger_blocked_max_independent(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          waiting_max: 5
          blocked_max: 2
        """,
    )
    t = load_triggers(ws)["handle-reply"]
    assert t.waiting_max == 5
    assert t.blocked_max == 2


def test_load_trigger_rejects_bad_concurrency(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          concurrency: 0
        """,
    )
    with pytest.raises(ConfigError, match="concurrency"):
        load_triggers(ws)


def test_load_trigger_rejects_bad_order(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          order: fifo
        """,
    )
    with pytest.raises(ConfigError, match="order"):
        load_triggers(ws)


def test_load_trigger_rejects_non_positive_cap(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          waiting_max: -1
        """,
    )
    with pytest.raises(ConfigError, match="waiting_max"):
        load_triggers(ws)


# --------------------------------------------------------------------------- #
# count_by_class
# --------------------------------------------------------------------------- #


def test_count_by_class_splits_waiting_kinds(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    _park_waiting(watch, "a.json", exit_code=6)
    _park_waiting(watch, "b.json", exit_code=6)
    _park_waiting(watch, "c.json", exit_code=9)
    _park_waiting(watch, "d.json", exit_code=8)
    (watch / "spool-1.json").write_text("x", encoding="utf-8")
    (watch / "spool-2.json").write_text("y", encoding="utf-8")
    claim = watch / ".claim"
    claim.mkdir()
    (claim / "live.json").write_text("z", encoding="utf-8")

    d = count_by_class(watch, glob="*.json")
    assert d["needs_human"] == 2
    assert d["blocked"] == 1
    assert d["capacity"] == 1
    assert d["waiting"] == 4
    assert d["claimed"] == 1
    assert d["inflight"] == 5  # 1 claimed + 4 waiting
    assert d["spool"] == 2


# --------------------------------------------------------------------------- #
# concurrency:1 + no caps == today
# --------------------------------------------------------------------------- #


def test_concurrency_1_no_caps_equivalence_with_serial_drain(tmp_path, monkeypatch):
    """Explicit D7 proof: default trigger drain matches pre-W3 claim order + retire."""
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
        """,
    )
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    for name in ("c.json", "a.json", "b.json"):
        (watch_abs / name).write_text(name, encoding="utf-8")

    runner = FakeRunner({("cairn", "run"): RunResult(0, "", "")})
    code = run_trigger("handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW)
    assert code == 0
    # Serial name order: a, b, c — spawn argv carries the claim path under .claim/
    claimed_names = []
    for call in runner.calls:
        argv = call["argv"]
        param = next(a for a in argv if a.startswith("event="))
        claimed_names.append(Path(param.split("=", 1)[1]).name)
    assert claimed_names == ["a.json", "b.json", "c.json"]
    assert (watch_abs / ".done" / "a.json").is_file()
    assert (watch_abs / ".done" / "b.json").is_file()
    assert (watch_abs / ".done" / "c.json").is_file()
    t = load_triggers(ws)["handle-reply"]
    assert t.concurrency == 1
    assert t.order == "name"
    assert t.waiting_max is None


# --------------------------------------------------------------------------- #
# Back pressure caps
# --------------------------------------------------------------------------- #


def test_waiting_max_stops_admission_exit_0(tmp_path, monkeypatch):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          waiting_max: 2
        """,
    )
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    _park_waiting(watch_abs, "park1.json", exit_code=6)
    _park_waiting(watch_abs, "park2.json", exit_code=6)
    (watch_abs / "new.json").write_text("should not claim", encoding="utf-8")

    err = io.StringIO()
    runner = FakeRunner({("cairn", "run"): RunResult(0, "", "")})
    code = run_trigger(
        "handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW, err=err
    )
    assert code == 0
    assert runner.calls == []
    assert (watch_abs / "new.json").is_file()  # still in inbox
    assert "review lane full (2 needs-human) — not claiming" in err.getvalue()


def test_blocked_max_stops_admission(tmp_path, monkeypatch):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          blocked_max: 1
        """,
    )
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    _park_waiting(watch_abs, "blocked.json", exit_code=9)
    (watch_abs / "new.json").write_text("x", encoding="utf-8")

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
    assert "blocked lane full (1 blocked) — not claiming" in err.getvalue()
    assert (watch_abs / "new.json").is_file()


def test_capacity_max_stops_admission(tmp_path, monkeypatch):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          capacity_max: 1
        """,
    )
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    _park_waiting(watch_abs, "cap.json", exit_code=8)
    (watch_abs / "new.json").write_text("x", encoding="utf-8")

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
    assert "capacity lane full (1 capacity) — not claiming" in err.getvalue()


def test_wip_max_across_mixed_states(tmp_path, monkeypatch):
    """wip = claimed + all waiting; mixed kinds still fill the budget."""
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          wip_max: 3
        """,
    )
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    _park_waiting(watch_abs, "h.json", exit_code=6)
    _park_waiting(watch_abs, "b.json", exit_code=9)
    claim = watch_abs / ".claim"
    claim.mkdir()
    (claim / "stuck.json").write_text("stuck", encoding="utf-8")
    (watch_abs / "new.json").write_text("x", encoding="utf-8")

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
    assert "wip full (3 inflight) — not claiming" in err.getvalue()
    assert (watch_abs / "new.json").is_file()


def test_waiting_max_admits_when_under_cap_then_stops_after_park(tmp_path, monkeypatch):
    """Lazy re-check: first claim allowed under cap; park fills lane; second not claimed."""
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          waiting_max: 1
        """,
    )
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    (watch_abs / "a.json").write_text("a", encoding="utf-8")
    (watch_abs / "b.json").write_text("b", encoding="utf-8")

    runner = FakeRunner({("cairn", "run"): RunResult(6, "parked\n", "")})
    err = io.StringIO()
    code = run_trigger(
        "handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW, err=err
    )
    assert code == 0
    assert len(runner.calls) == 1  # only a.json admitted
    assert (watch_abs / ".waiting" / "a.json").is_file()
    assert (watch_abs / "b.json").is_file()  # not claimed
    assert "review lane full" in err.getvalue()


# --------------------------------------------------------------------------- #
# order: aged
# --------------------------------------------------------------------------- #


def test_order_name_unchanged(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    for n in ("p9-old.json", "p1-fresh.json", "p5-mid.json"):
        (watch / n).write_text("x", encoding="utf-8")
    cands = scan_candidates(watch, "*.json")
    ordered = order_candidates(cands, "name", now=NOW)
    assert [p.name for p in ordered] == ["p1-fresh.json", "p5-mid.json", "p9-old.json"]


def test_order_aged_old_low_prio_before_fresh_high(tmp_path):
    """p9 aged past the formula sorts before a fresh p1.

    Formula: effective = declared_prio − age_seconds / AGE_STEP_SECONDS (1h/step).
    p9 with age 9h → effective 0; fresh p1 → effective 1; 0 < 1 so p9 first.
    """
    watch = tmp_path / "inbox"
    watch.mkdir()
    old = watch / "p9-old.json"
    fresh = watch / "p1-fresh.json"
    old.write_text("old", encoding="utf-8")
    fresh.write_text("fresh", encoding="utf-8")
    now_ts = NOW.timestamp()
    # 9 hours ago
    os.utime(old, (now_ts - 9 * AGE_STEP_SECONDS, now_ts - 9 * AGE_STEP_SECONDS))
    os.utime(fresh, (now_ts, now_ts))

    assert effective_prio(old, now_ts=now_ts) == pytest.approx(0.0)
    assert effective_prio(fresh, now_ts=now_ts) == pytest.approx(1.0)

    cands = scan_candidates(watch, "*.json")
    ordered = order_candidates(cands, "aged", now=NOW)
    assert [p.name for p in ordered] == ["p9-old.json", "p1-fresh.json"]


def test_order_aged_drain_claims_old_p9_first(tmp_path, monkeypatch):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          order: aged
        """,
    )
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    old = watch_abs / "p9-old.json"
    fresh = watch_abs / "p1-fresh.json"
    old.write_text("old", encoding="utf-8")
    fresh.write_text("fresh", encoding="utf-8")
    now_ts = NOW.timestamp()
    os.utime(old, (now_ts - 9 * AGE_STEP_SECONDS, now_ts - 9 * AGE_STEP_SECONDS))
    os.utime(fresh, (now_ts, now_ts))

    runner = FakeRunner({("cairn", "run"): RunResult(0, "", "")})
    # Only admit the first by failing the second? No — both should run; check order.
    code = run_trigger("handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW)
    assert code == 0
    first_param = next(a for a in runner.calls[0]["argv"] if a.startswith("event="))
    assert "p9-old.json" in first_param


# --------------------------------------------------------------------------- #
# concurrency > 1
# --------------------------------------------------------------------------- #


def test_concurrency_pool_bounds_and_retires_all(tmp_path, monkeypatch):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          concurrency: 3
        """,
    )
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    for i in range(6):
        (watch_abs / f"item-{i}.json").write_text(str(i), encoding="utf-8")

    live = 0
    peak = 0
    lock = threading.Lock()

    class SlowRunner(RunnerBase):
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
                    self._pid = 100 + len(self_outer.calls)

                @property
                def pid(self):
                    return self._pid

                def wait(self, timeout=None):
                    time.sleep(0.05)
                    with lock:
                        nonlocal live
                        live -= 1
                    return RunResult(returncode=0, stdout="", stderr="")

                def poll(self):
                    return None

                def terminate(self):
                    return None

            self_outer = self
            return SlowHandle()

    runner = SlowRunner()
    code = run_trigger("handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW)
    assert code == 0
    assert peak <= 3
    assert peak >= 2  # actually concurrent
    assert len(runner.calls) == 6
    done = list((watch_abs / ".done").glob("item-*.json"))
    assert len(done) == 6


def test_concurrency_pool_no_double_claim(tmp_path, monkeypatch):
    """Two concurrent claims never collide on the same candidate (lost-race discipline)."""
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          concurrency: 4
        """,
    )
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    for i in range(8):
        (watch_abs / f"e{i}.json").write_text(str(i), encoding="utf-8")

    claimed_params: list[str] = []
    lock = threading.Lock()

    class RecordingRunner(RunnerBase):
        def spawn(self, argv, *, input=None, cwd=None):
            param = next(a for a in argv if a.startswith("event="))
            with lock:
                claimed_params.append(param)
            time.sleep(0.01)
            return _CannedHandle(RunResult(0, "", ""))

    code = run_trigger(
        "handle-reply", ws, runner=RecordingRunner(), cairn_bin="cairn", now=NOW
    )
    assert code == 0
    # Unique claim paths — never two workers on the same claim file.
    assert len(claimed_params) == len(set(claimed_params)) == 8


# --------------------------------------------------------------------------- #
# trigger list depths
# --------------------------------------------------------------------------- #


def test_trigger_list_shows_class_depths_and_caps(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          waiting_max: 5
          concurrency: 2
          order: aged
          inbox_max: 10
        """,
    )
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    _park_waiting(watch_abs, "h.json", exit_code=6)
    _park_waiting(watch_abs, "b.json", exit_code=9)
    (watch_abs / "spool.json").write_text("s", encoding="utf-8")

    statuses = list_installed_triggers(
        ws, backend="launchd", runner=FakeRunner(), launchd_dir=tmp_path / "launchd"
    )
    assert len(statuses) == 1
    s = statuses[0]
    assert s.needs_human == 1
    assert s.blocked == 1
    assert s.capacity == 0
    assert s.inflight == 2
    assert s.spool == 1
    assert s.waiting_max == 5
    assert s.blocked_max == 5
    assert s.concurrency == 2
    assert s.order == "aged"
    assert s.inbox_max == 10
