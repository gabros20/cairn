"""queue_ledger.sweep — advance .waiting/ from trail evidence (FACTORY-PLAN T6)."""

from __future__ import annotations

import json
from pathlib import Path

from cairn.kernel.queue_ledger import (
    claim,
    pointer_path,
    read_pointer,
    retire,
    sweep,
    write_pointer,
)
from cairn.kernel.types import OutcomeClass, RunOutcome, classify_exit


def _watch(tmp_path: Path) -> Path:
    d = tmp_path / "inbox"
    d.mkdir()
    return d


def _park(watch: Path, name: str, run_dir: Path, *, exit_code: int = 6) -> Path:
    item = watch / name
    item.write_text("payload", encoding="utf-8")
    claimed = claim(watch, item)
    assert claimed is not None
    write_pointer(
        pointer_path(claimed.parent, claimed.name),
        run_dir=run_dir,
        child_pid=11,
    )
    parked = retire(
        watch,
        claimed,
        outcome=classify_exit(exit_code),
        on_done="done",
        exit_code=exit_code,
        child_pid=11,
        run_dir=run_dir,
    )
    assert parked is not None
    return parked


def _write_trail(run_dir: Path, events: list[dict]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text('{"status":"halted"}', encoding="utf-8")
    lines = []
    for i, ev in enumerate(events, start=1):
        doc = {"seq": i, **ev}
        lines.append(json.dumps(doc))
    (run_dir / "trail.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_sweep_parked_answered_resumed_done_retires(tmp_path):
    watch = _watch(tmp_path)
    run_dir = tmp_path / "runs" / "t-one"
    parked = _park(watch, "one.json", run_dir, exit_code=6)
    # After park, an external resume finishes the run (trail ends with run-done).
    _write_trail(run_dir, [
        {"event": "run-halt", "data": {"exit_code": 6, "reason": "manual"}},
        {"event": "run-done", "data": {"nodes": 1}},
    ])
    report = sweep(watch, on_done="done")
    assert parked.name in {p.name for p in report.moved} or not (watch / ".waiting" / "one.json").exists()
    assert (watch / ".done" / "one.json").is_file()
    assert (watch / ".done" / "tombstones" / "one.json").is_file()
    assert not list(report.left)


def test_sweep_failure_halt_moves_to_failed(tmp_path):
    watch = _watch(tmp_path)
    run_dir = tmp_path / "runs" / "t-bad"
    _park(watch, "bad.json", run_dir, exit_code=6)
    _write_trail(run_dir, [
        {"event": "run-halt", "data": {"exit_code": 4, "reason": "executor"}},
    ])
    report = sweep(watch, on_done="done")
    assert (watch / ".failed" / "bad.json").is_file()
    assert (watch / ".done" / "tombstones" / "bad.json").is_file()
    assert any(p.name == "bad.json" for p in report.moved)


def test_sweep_needs_human_stays(tmp_path):
    watch = _watch(tmp_path)
    run_dir = tmp_path / "runs" / "t-gate"
    _park(watch, "gate.json", run_dir, exit_code=6)
    _write_trail(run_dir, [
        {"event": "run-halt", "data": {"exit_code": 6, "reason": "manual"}},
    ])
    report = sweep(watch, on_done="done")
    assert (watch / ".waiting" / "gate.json").is_file()
    assert any(p.name == "gate.json" for p in report.left)
    assert not report.moved


def test_sweep_capacity_and_blocked_stay(tmp_path):
    watch = _watch(tmp_path)
    for name, code in (("cap.json", 8), ("blk.json", 9)):
        rd = tmp_path / "runs" / f"t-{name}"
        _park(watch, name, rd, exit_code=code)
        _write_trail(rd, [{"event": "run-halt", "data": {"exit_code": code}}])
    report = sweep(watch, on_done="done")
    assert (watch / ".waiting" / "cap.json").is_file()
    assert (watch / ".waiting" / "blk.json").is_file()
    assert len(report.left) == 2


def test_sweep_capacity_resumes_when_free_slots(tmp_path):
    """W6-T2: CAPACITY park is re-driven when free_slots > 0; trail run-done → retire."""
    watch = _watch(tmp_path)
    run_dir = tmp_path / "runs" / "t-cap"
    _park(watch, "cap.json", run_dir, exit_code=8)
    _write_trail(run_dir, [{"event": "run-halt", "data": {"exit_code": 8}}])

    resumed: list[Path] = []

    def resume(rd: Path) -> None:
        resumed.append(rd)
        # Simulate a successful resume that finishes the run.
        _write_trail(
            rd,
            [
                {"event": "run-halt", "data": {"exit_code": 8}},
                {"event": "run-done", "data": {"nodes": 1}},
            ],
        )

    report = sweep(
        watch,
        on_done="done",
        free_slots=1,
        resume_capacity=resume,
    )
    assert resumed == [run_dir]
    assert (watch / ".done" / "cap.json").is_file()
    assert not (watch / ".waiting" / "cap.json").exists()
    assert any(p.name == "cap.json" for p in report.capacity_resumed)
    assert any(p.name == "cap.json" for p in report.moved)


def test_sweep_capacity_stays_when_no_free_slots(tmp_path):
    """W6-T2: zero free slots → leave capacity park for the next beat (no strand forever only when free)."""
    watch = _watch(tmp_path)
    run_dir = tmp_path / "runs" / "t-cap0"
    _park(watch, "cap.json", run_dir, exit_code=8)
    _write_trail(run_dir, [{"event": "run-halt", "data": {"exit_code": 8}}])
    called: list[Path] = []

    report = sweep(
        watch,
        on_done="done",
        free_slots=0,
        resume_capacity=lambda rd: called.append(rd),
    )
    assert called == []
    assert (watch / ".waiting" / "cap.json").is_file()
    assert any(p.name == "cap.json" for p in report.left)
    assert report.capacity_resumed == ()


def test_sweep_capacity_resume_budget_limits_herd(tmp_path):
    """At most free_slots capacity parks resumed per beat (FACTORY-PLAN T6 budget)."""
    watch = _watch(tmp_path)
    for i in range(3):
        rd = tmp_path / "runs" / f"t-c{i}"
        _park(watch, f"c{i}.json", rd, exit_code=8)
        _write_trail(rd, [{"event": "run-halt", "data": {"exit_code": 8}}])

    resumed: list[str] = []

    def resume(rd: Path) -> None:
        resumed.append(rd.name)
        _write_trail(
            rd,
            [
                {"event": "run-halt", "data": {"exit_code": 8}},
                {"event": "run-done", "data": {"nodes": 1}},
            ],
        )

    report = sweep(
        watch,
        on_done="done",
        free_slots=2,
        resume_capacity=resume,
    )
    assert len(resumed) == 2
    assert len(report.capacity_resumed) == 2
    # One capacity park left for the next beat.
    waiting = list((watch / ".waiting").iterdir()) if (watch / ".waiting").is_dir() else []
    assert sum(1 for p in waiting if p.is_file()) == 1


def test_sweep_blocked_not_resumed_by_capacity_path(tmp_path):
    """BLOCKED parks are not capacity-resumed even when free_slots > 0."""
    watch = _watch(tmp_path)
    run_dir = tmp_path / "runs" / "t-blk"
    _park(watch, "blk.json", run_dir, exit_code=9)
    _write_trail(run_dir, [{"event": "run-halt", "data": {"exit_code": 9}}])
    called: list[Path] = []
    report = sweep(
        watch,
        on_done="done",
        free_slots=5,
        resume_capacity=lambda rd: called.append(rd),
    )
    assert called == []
    assert (watch / ".waiting" / "blk.json").is_file()
    assert report.capacity_resumed == ()


def test_sweep_vanished_run_dir_fails_with_gc_diagnostic(tmp_path):
    watch = _watch(tmp_path)
    run_dir = tmp_path / "runs" / "t-gone"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("{}", encoding="utf-8")
    _park(watch, "gone.json", run_dir, exit_code=6)
    # Simulate gc removing the run dir.
    for p in run_dir.iterdir():
        p.unlink()
    run_dir.rmdir()
    report = sweep(watch, on_done="done")
    assert (watch / ".failed" / "gone.json").is_file()
    assert any("gc" in d for d in report.diagnostics)
    assert any("vanished" in d for d in report.diagnostics)


def test_sweep_item_without_pointer_left_as_stuck(tmp_path):
    watch = _watch(tmp_path)
    waiting = watch / ".waiting"
    waiting.mkdir()
    (waiting / "orphan.json").write_text("x", encoding="utf-8")
    report = sweep(watch, on_done="done")
    assert (waiting / "orphan.json").is_file()
    assert any("no pointer" in d for d in report.diagnostics)
    assert any(p.name == "orphan.json" for p in report.left)


def test_sweep_repairs_pointer_moved_item_not(tmp_path):
    """T5 crash: pointer in .waiting/.runs/, item still in .claim/ → complete move."""
    watch = _watch(tmp_path)
    claim_dir = watch / ".claim"
    claim_dir.mkdir()
    item = claim_dir / "mid.json"
    item.write_text("body", encoding="utf-8")
    run_dir = tmp_path / "runs" / "t-mid"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text('{"status":"halted"}', encoding="utf-8")
    waiting = watch / ".waiting"
    waiting.mkdir()
    write_pointer(
        pointer_path(waiting, "mid.json"),
        run_dir=run_dir,
        outcome="waiting",
        exit_code=6,
        child_pid=1,
    )
    # Still-waiting trail so after repair the item is left (not re-retired).
    _write_trail(run_dir, [{"event": "run-halt", "data": {"exit_code": 6}}])

    report = sweep(watch, on_done="done")
    assert (waiting / "mid.json").is_file()
    assert not item.exists()
    assert any(p.name == "mid.json" for p in report.repaired)
    # Still waiting-class → left after repair.
    assert (waiting / "mid.json").is_file()
