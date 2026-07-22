"""T6 r2 — lost-race discipline, corrupt-pointer quarantine, RunExists diagnostic.

Concurrent drains on one watch dir are legal (plan §2 T2 bounded overshoot). These
tests simulate competing moves (real hardlinks / injected faults) and assert the
loser is benign: no -v2 duplicates, no stuck claims, sweep continues past one
item's hazard.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cairn.kernel.proc import RunResult, RunnerBase
from cairn.kernel.queue_ledger import (
    _place,
    _relocate_pointer,
    _repair_pointer_item_pair,
    claim,
    pointer_path,
    read_pointer,
    retire,
    sweep,
    write_pointer,
)
from cairn.kernel.types import OutcomeClass, RunOutcome, classify_exit
from fstestkit import RecordingFs


def _watch(tmp_path: Path) -> Path:
    d = tmp_path / "inbox"
    d.mkdir()
    return d


# --------------------------------------------------------------------------- #
# C1 — _place lost-race (same inode at dest → no -v2)
# --------------------------------------------------------------------------- #


def test_place_same_source_race_returns_existing_dest_no_v2(tmp_path):
    """Loser of a concurrent place: dest already same inode → return dest, no -v2."""
    watch = _watch(tmp_path)
    candidate = watch / "event.json"
    candidate.write_text("body", encoding="utf-8")
    claimed = claim(watch, candidate)
    assert claimed is not None

    dest_dir = watch / ".done"
    dest_dir.mkdir()
    # Winner already hard-linked claim → dest under the original name.
    winner_dest = dest_dir / "event.json"
    import os

    os.link(claimed, winner_dest)

    placed = _place(claimed, dest_dir, "event.json")
    assert placed == winner_dest
    assert not (dest_dir / "event-v2.json").exists()
    assert not claimed.exists()  # source unlinked (tolerated if already gone)


def test_place_genuine_collision_still_gets_v2(tmp_path):
    """Different content already at dest → -v2 (not a same-source race)."""
    watch = _watch(tmp_path)
    candidate = watch / "event.json"
    candidate.write_text("new", encoding="utf-8")
    claimed = claim(watch, candidate)
    dest_dir = watch / ".done"
    dest_dir.mkdir()
    (dest_dir / "event.json").write_text("other-event", encoding="utf-8")

    placed = _place(claimed, dest_dir, "event.json")
    assert placed == dest_dir / "event-v2.json"
    assert (dest_dir / "event.json").read_text(encoding="utf-8") == "other-event"


def test_retire_lost_race_no_v2_no_pointer_orphan(tmp_path):
    """Two concurrent DONE retires of same claim: loser is benign, one tombstone."""
    watch = _watch(tmp_path)
    candidate = watch / "event.json"
    candidate.write_text("body", encoding="utf-8")
    claimed = claim(watch, candidate)
    run_dir = tmp_path / "runs" / "t-event"
    run_dir.mkdir(parents=True)
    write_pointer(
        pointer_path(claimed.parent, claimed.name),
        run_dir=run_dir,
        child_pid=1,
    )

    # Simulate winner already finishing place into .done/ before loser links.
    done = watch / ".done"
    done.mkdir()
    import os

    os.link(claimed, done / "event.json")

    dest = retire(
        watch,
        claimed,
        outcome=RunOutcome(outcome=OutcomeClass.DONE),
        on_done="done",
        exit_code=0,
        child_pid=1,
        run_dir=run_dir,
    )
    assert dest == done / "event.json"
    assert not (done / "event-v2.json").exists()
    assert (done / "tombstones" / "event.json").is_file()
    # No orphan pointer left in claim/.runs
    claim_runs = watch / ".claim" / ".runs"
    if claim_runs.is_dir():
        assert list(claim_runs.iterdir()) == []


def test_relocate_pointer_source_already_unlinked_is_benign(tmp_path):
    """Loser of pointer relocate: source already gone → no raise."""
    watch = _watch(tmp_path)
    claim_dir = watch / ".claim"
    claim_dir.mkdir()
    src = pointer_path(claim_dir, "event.json")
    write_pointer(src, run_dir="/runs/t", outcome=None, child_pid=1)
    dest = pointer_path(watch / ".waiting", "event.json")
    dest.parent.mkdir(parents=True)
    # Winner already wrote dest and dropped source.
    write_pointer(dest, run_dir="/runs/t", outcome="waiting", exit_code=6, child_pid=1)
    src.unlink()

    # Loser's relocate: write dest again, try unlink missing src.
    _relocate_pointer(
        src,
        dest,
        run_dir="/runs/t",
        outcome="waiting",
        exit_code=6,
        child_pid=1,
    )
    assert dest.is_file()
    assert read_pointer(dest)["outcome"] == "waiting"


def test_repair_race_dest_already_placed_is_benign(tmp_path):
    """Concurrent repair: dest already has item → no raise, leftover source cleaned."""
    watch = _watch(tmp_path)
    claim_dir = watch / ".claim"
    claim_dir.mkdir()
    waiting = watch / ".waiting"
    waiting.mkdir()
    src_item = claim_dir / "mid.json"
    src_item.write_text("body", encoding="utf-8")
    dest_item = waiting / "mid.json"
    import os

    os.link(src_item, dest_item)  # winner already completed the item move
    ptr = pointer_path(waiting, "mid.json")
    write_pointer(ptr, run_dir="/runs/x", outcome="waiting", exit_code=6, child_pid=1)

    result, diag = _repair_pointer_item_pair(
        watch, name="mid.json", pointer=ptr, on_done="done"
    )
    # Consistent pair after cleanup of leftover source.
    assert (waiting / "mid.json").is_file()
    assert not src_item.exists()
    assert result is None  # already consistent after cleanup
    assert diag is None


def test_sweep_item_hazard_does_not_abort_rest(tmp_path, monkeypatch):
    """Per-item isolation: item A's hazard is diagnosed; item B still retires."""
    watch = _watch(tmp_path)
    waiting = watch / ".waiting"
    waiting.mkdir()

    # Item A: will hazard on retire.
    a = waiting / "a.json"
    a.write_text("a", encoding="utf-8")
    write_pointer(
        pointer_path(waiting, "a.json"),
        run_dir=str(tmp_path / "runs" / "gone-a"),
        outcome="waiting",
        exit_code=6,
        child_pid=1,
    )
    # Item B: vanished run dir → clean FAIL retire.
    b = waiting / "b.json"
    b.write_text("b", encoding="utf-8")
    write_pointer(
        pointer_path(waiting, "b.json"),
        run_dir=str(tmp_path / "runs" / "gone-b"),
        outcome="waiting",
        exit_code=6,
        child_pid=1,
    )

    import cairn.kernel.queue_ledger as ql

    real_retire = ql.retire
    calls = {"n": 0}

    def flaky_retire(watch_abs, claim_path, **kwargs):
        calls["n"] += 1
        if claim_path.name == "a.json":
            raise OSError("simulated retire hazard on a")
        return real_retire(watch_abs, claim_path, **kwargs)

    monkeypatch.setattr(ql, "retire", flaky_retire)

    report = sweep(watch, on_done="done")
    assert any("a.json" in d and "hazarded" in d for d in report.diagnostics)
    assert (watch / ".failed" / "b.json").is_file()
    assert any(p.name == "b.json" for p in report.moved)
    # a still in waiting (left after hazard) or diagnosed
    assert a.is_file() or any("a.json" in d for d in report.diagnostics)


# --------------------------------------------------------------------------- #
# I2 — corrupt pointer quarantine vs well-formed orphan delete
# --------------------------------------------------------------------------- #


def test_repair_corrupt_pointer_quarantined_not_silently_deleted(tmp_path):
    watch = _watch(tmp_path)
    runs = watch / ".waiting" / ".runs"
    runs.mkdir(parents=True)
    ptr = runs / "broken.json"
    raw = "NOT-JSON{{{{"
    ptr.write_text(raw, encoding="utf-8")

    result, diag = _repair_pointer_item_pair(
        watch, name="broken.json", pointer=ptr, on_done="done"
    )
    assert result is None
    assert diag is not None and "corrupt" in diag
    assert not ptr.exists()
    quarantine = watch / ".failed" / ".runs" / "broken.json.corrupt"
    assert quarantine.is_file()
    assert quarantine.read_text(encoding="utf-8") == raw


def test_repair_wellformed_orphan_pointer_deleted(tmp_path):
    watch = _watch(tmp_path)
    runs = watch / ".claim" / ".runs"
    runs.mkdir(parents=True)
    ptr = runs / "orphan.json"
    write_pointer(ptr, run_dir=str(tmp_path / "runs" / "missing"), outcome="waiting")

    result, diag = _repair_pointer_item_pair(
        watch, name="orphan.json", pointer=ptr, on_done="done"
    )
    assert result is None
    assert diag is not None and "orphan" in diag and "no validated" in diag
    assert not ptr.exists()
    assert not (watch / ".failed" / ".runs" / "orphan.json.corrupt").exists()


# --------------------------------------------------------------------------- #
# I1 — same-name re-drop → RunExists diagnostic, item back in inbox
# --------------------------------------------------------------------------- #


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

    def spawn(self, argv, *, input=None, cwd=None) -> _CannedHandle:
        self.calls.append({"argv": list(argv)})
        key = tuple(argv[:2])
        result = self._canned.get(key, RunResult(returncode=0, stdout="", stderr=""))
        return _CannedHandle(result)


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def test_run_trigger_redrop_same_name_leaves_item_with_run_dir_exists_diagnostic(
    tmp_path, monkeypatch
):
    """drop → done → re-drop same name → exit 0, diagnostic, item stays in inbox."""
    from cairn.kernel.queue_drain import run_trigger
    from cairn.kernel.runctl import Minted
    from cairn.kernel.runstate import RunExistsError
    import cairn.kernel.queue_drain as qd

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "pipelines").mkdir()
    (ws / "pipelines" / "handle-reply.yaml").write_text("nodes: {}\n", encoding="utf-8")
    (ws / "triggers.yaml").write_text(
        "handle-reply:\n  pipeline: handle-reply\n  watch: inbox/replies\n",
        encoding="utf-8",
    )
    watch = ws / "inbox" / "replies"
    watch.mkdir(parents=True)

    calls = {"n": 0}

    def fake_preallocate(workspace_dir, trigger, claimed_path, *, now):
        calls["n"] += 1
        run_dir = qd.run_dir_for_item(ws / "runs", trigger.name, claimed_path.name)
        if calls["n"] == 1:
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run.json").write_text('{"status":"running"}', encoding="utf-8")
            return Minted(run_dir=run_dir)
        # Second drop of same name: run dir already exists.
        raise RunExistsError(f"run dir already exists: {run_dir}")

    monkeypatch.setattr(qd, "preallocate_run", fake_preallocate)

    # First firing → done.
    (watch / "one.json").write_text("first", encoding="utf-8")
    err1 = io.StringIO()
    code1 = run_trigger(
        "handle-reply",
        ws,
        runner=FakeRunner({("cairn", "run"): RunResult(0, "", "")}),
        cairn_bin="cairn",
        now=NOW,
        err=err1,
    )
    assert code1 == 0
    assert (watch / ".done" / "one.json").is_file()

    # Re-drop same filename.
    (watch / "one.json").write_text("second", encoding="utf-8")
    err2 = io.StringIO()
    code2 = run_trigger(
        "handle-reply",
        ws,
        runner=FakeRunner({("cairn", "run"): RunResult(0, "", "")}),
        cairn_bin="cairn",
        now=NOW,
        err=err2,
    )
    assert code2 == 0  # not a drain failure
    assert (watch / "one.json").is_file()  # back in inbox
    assert not (watch / ".claim" / "one.json").exists()  # not stuck
    diag = err2.getvalue()
    assert "run-dir-exists" in diag or "run dir already exists" in diag
    assert "W3" in diag
    assert "handle-reply-one" in diag
