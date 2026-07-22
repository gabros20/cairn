"""W1d cross-module: reciprocal queue pin lifecycle + vanished-run gc regression.

Covers mint→pin→gc protect→retire clear→gc collect, and drop→park→gc→sweep
still finds the run (the poison path fixed by outcome + pin protection).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cairn.kernel.gckit import (
    QUEUE_PIN_NAME,
    apply_gc,
    clear_queue_pin,
    plan_gc,
    read_queue_pin,
    write_queue_pin,
)
from cairn.kernel.queue_ledger import (
    claim,
    pointer_path,
    retire,
    sweep,
    write_pointer,
)
from cairn.kernel.trail import TrailWriter, format_at
from cairn.kernel.types import classify_exit

NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _minted_run(
    runs_root: Path,
    run_id: str,
    *,
    trigger: str = "on-hook",
    item: str = "p1-src-id-r1.json",
    age_days: float = 30.0,
    halt_exit: int | None = None,
    done: bool = False,
) -> Path:
    """Fabricate a queue-minted run: run.json + trail + pin (as preallocate_run would)."""
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    created = NOW - timedelta(days=age_days)
    status = "done" if done else ("halted" if halt_exit is not None else "running")
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "pipeline": "p",
                "created_at": _iso(created),
                "status": status,
            }
        ),
        encoding="utf-8",
    )
    w = TrailWriter(run_dir, run_id)
    w.emit("run-start")
    if halt_exit is not None:
        w.emit("run-halt", data={"exit_code": halt_exit, "reason": "park"})
    if done:
        w.emit("run-done")
    w.close()
    (run_dir / "logs").mkdir(exist_ok=True)
    (run_dir / "logs" / "cap.log").write_text("evidence" * 50, encoding="utf-8")
    write_queue_pin(
        run_dir,
        trigger=trigger,
        item=item,
        pinned_at=format_at(created),
    )
    return run_dir


def test_pin_lifecycle_mint_protects_retire_done_releases(tmp_path):
    """Minted run has pin → gc refuses full+slim → retire DONE clears pin → gc collects."""
    runs = tmp_path / "runs"
    watch = tmp_path / "inbox"
    watch.mkdir()
    runs.mkdir()
    run_dir = _minted_run(runs, "on-hook-item", halt_exit=None, done=True)
    assert (run_dir / QUEUE_PIN_NAME).is_file()
    pin = read_queue_pin(run_dir)
    assert pin is not None
    assert pin["trigger"] == "on-hook"
    assert pin["item"] == "p1-src-id-r1.json"

    # While pinned, neither full delete nor artifacts-only may select it.
    assert plan_gc(runs, keep_days=0, now=NOW).candidates == []
    assert plan_gc(runs, keep_days=0, artifacts_only=True, now=NOW).candidates == []

    # Terminal retire DONE (as drain/sweep would) — pin cleared after ledger placement.
    item = watch / "p1-src-id-r1.json"
    item.write_text("payload", encoding="utf-8")
    claimed = claim(watch, item)
    assert claimed is not None
    write_pointer(
        pointer_path(claimed.parent, claimed.name),
        run_dir=run_dir,
        child_pid=42,
    )
    placed = retire(
        watch,
        claimed,
        outcome=classify_exit(0),
        on_done="done",
        exit_code=0,
        child_pid=42,
        run_dir=run_dir,
    )
    assert placed is not None
    assert (watch / ".done" / "p1-src-id-r1.json").is_file()
    assert not (run_dir / QUEUE_PIN_NAME).exists()

    plan = plan_gc(runs, keep_days=0, now=NOW)
    assert [c.run_id for c in plan.candidates] == ["on-hook-item"]
    result = apply_gc(plan)
    assert result.deleted == ["on-hook-item"]
    assert not run_dir.exists()


def test_pin_cleared_on_failed_retire_not_on_waiting(tmp_path):
    runs = tmp_path / "runs"
    watch = tmp_path / "inbox"
    watch.mkdir()
    runs.mkdir()

    # FAILED retire clears pin.
    fail_dir = _minted_run(runs, "t-fail", item="fail.json", halt_exit=4)
    item = watch / "fail.json"
    item.write_text("x", encoding="utf-8")
    claimed = claim(watch, item)
    write_pointer(pointer_path(claimed.parent, claimed.name), run_dir=fail_dir)
    retire(
        watch,
        claimed,
        outcome=classify_exit(4),
        on_done="done",
        exit_code=4,
        run_dir=fail_dir,
    )
    assert not (fail_dir / QUEUE_PIN_NAME).exists()

    # WAITING retire retains pin (judgment pending).
    wait_dir = _minted_run(runs, "t-wait", item="wait.json", halt_exit=6)
    item2 = watch / "wait.json"
    item2.write_text("y", encoding="utf-8")
    claimed2 = claim(watch, item2)
    write_pointer(pointer_path(claimed2.parent, claimed2.name), run_dir=wait_dir)
    retire(
        watch,
        claimed2,
        outcome=classify_exit(6),
        on_done="done",
        exit_code=6,
        run_dir=wait_dir,
    )
    assert (wait_dir / QUEUE_PIN_NAME).is_file()
    assert (watch / ".waiting" / "wait.json").is_file()


def test_stale_pin_after_crash_still_protects(tmp_path):
    """Simulate retire crash before pin clear: terminal ledger done, pin left."""
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = _minted_run(runs, "stale", done=True)
    # Operator-side: ledger already terminal (we just leave the pin).
    assert (run_dir / QUEUE_PIN_NAME).is_file()

    plan = plan_gc(runs, keep_days=0, now=NOW)
    assert plan.candidates == []
    assert any("queue-pinned" in r for _, r in plan.skipped)

    # After explicit clear (what reconcile/W3 would do), collectable.
    clear_queue_pin(run_dir)
    plan2 = plan_gc(runs, keep_days=0, now=NOW)
    assert [c.run_id for c in plan2.candidates] == ["stale"]


def test_waiting_park_survives_gc_then_sweep_retires(tmp_path):
    """Regression: gc --keep-days 0 must not vanish a .waiting-pointed run.

    drop → park (waiting + pin + halt 6) → gc → sweep still finds run → retires cleanly.
    """
    runs = tmp_path / "runs"
    watch = tmp_path / "inbox"
    watch.mkdir()
    runs.mkdir()
    run_dir = _minted_run(
        runs,
        "on-hook-p1-src-id-r1",
        trigger="on-hook",
        item="p1-src-id-r1.json",
        halt_exit=6,
    )

    item = watch / "p1-src-id-r1.json"
    item.write_text('{"id":1}', encoding="utf-8")
    claimed = claim(watch, item)
    write_pointer(
        pointer_path(claimed.parent, claimed.name),
        run_dir=run_dir,
        child_pid=7,
    )
    parked = retire(
        watch,
        claimed,
        outcome=classify_exit(6),
        on_done="done",
        exit_code=6,
        child_pid=7,
        run_dir=run_dir,
    )
    assert parked is not None
    assert (watch / ".waiting" / "p1-src-id-r1.json").is_file()
    assert (run_dir / QUEUE_PIN_NAME).is_file()

    # The bug: keep-days 0 deleted halted parks; sweep then saw vanished run → poison.
    plan = plan_gc(runs, keep_days=0, now=NOW)
    assert plan.candidates == []
    result = apply_gc(plan)
    assert result.deleted == []
    assert run_dir.is_dir()
    assert (run_dir / "run.json").is_file()

    # Human answers / resume finishes: trail ends run-done; sweep retires cleanly.
    w = TrailWriter(run_dir, run_dir.name)
    w.emit("run-done")
    w.close()
    report = sweep(watch, on_done="done")
    assert (watch / ".done" / "p1-src-id-r1.json").is_file()
    assert not (watch / ".waiting" / "p1-src-id-r1.json").exists()
    assert not (run_dir / QUEUE_PIN_NAME).exists()  # terminal retire cleared pin
    assert not any("vanished" in d for d in report.diagnostics)
    assert not any("gc" in d for d in report.diagnostics)
