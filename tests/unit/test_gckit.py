"""cairn gc — explicit retention over the runs root (API.md; SECURITY.md §5).

Behaviour tests against the public surface (plan_gc / apply_gc). Retention is NEVER
automatic and NEVER deletes a live/locked/needs-human run; dry-run (plan only) is the
default and deletion happens solely through apply_gc on an explicit plan.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from cairn.kernel.gckit import GcPlan, GcResult, apply_gc, plan_gc
from cairn.kernel.runstate import run_lock
from cairn.kernel.trail import TrailWriter

NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _make_run(
    runs_root,
    run_id,
    *,
    pipeline="brease-rebuild",
    status="done",
    age_days=0.0,
    last_event="run-done",
    artifacts=True,
):
    """Fabricate a run dir: run.json (status + created_at) + a trail whose last event
    sets the derived status, plus some heavyweight artifact/log payloads."""
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    created = NOW - timedelta(days=age_days)
    run_dir.joinpath("run.json").write_text(
        json.dumps({"run_id": run_id, "pipeline": pipeline, "created_at": _iso(created), "status": status})
    )
    w = TrailWriter(run_dir, run_id)
    w.emit("run-start")
    if last_event:
        w.emit(last_event)
    w.close()
    if artifacts:
        (run_dir / "logs").mkdir(exist_ok=True)
        (run_dir / "logs" / "capture.log").write_text("x" * 1000)
        (run_dir / "artifacts").mkdir(exist_ok=True)
        (run_dir / "artifacts" / "site-map.json").write_text("{}")
    return run_dir


def test_keep_days_selects_runs_older_than_n_and_dry_run_deletes_nothing(tmp_path):
    _make_run(tmp_path, "old-20260601", age_days=30)
    _make_run(tmp_path, "fresh-20260703", age_days=1)

    plan = plan_gc(tmp_path, keep_days=7, now=NOW)

    assert isinstance(plan, GcPlan)
    assert [c.run_id for c in plan.candidates] == ["old-20260601"]
    # plan_gc is a pure preview — both run dirs still exist afterwards.
    assert (tmp_path / "old-20260601").exists()
    assert (tmp_path / "fresh-20260703").exists()


def test_keep_last_keeps_newest_m_per_pipeline(tmp_path):
    _make_run(tmp_path, "a-20260601", pipeline="alpha", age_days=30)
    _make_run(tmp_path, "a-20260615", pipeline="alpha", age_days=15)
    _make_run(tmp_path, "a-20260702", pipeline="alpha", age_days=1)
    _make_run(tmp_path, "b-20260601", pipeline="beta", age_days=30)

    plan = plan_gc(tmp_path, keep_last=2, now=NOW)

    # alpha: newest 2 (a-20260615, a-20260702) kept → only a-20260601 selected.
    # beta: only 1 run, within newest-2 → kept.
    assert [c.run_id for c in plan.candidates] == ["a-20260601"]


def test_keep_days_and_keep_last_union_of_retention(tmp_path):
    # Old but among the newest-M → retained by keep_last despite failing keep_days.
    _make_run(tmp_path, "old-newest-20260601", pipeline="p", age_days=40)
    _make_run(tmp_path, "old-older-20260501", pipeline="p", age_days=60)
    _make_run(tmp_path, "fresh-20260702", pipeline="p", age_days=1)

    plan = plan_gc(tmp_path, keep_days=7, keep_last=2, now=NOW)

    # Kept: fresh (recent) + old-newest (newest-2). Deleted: only old-older.
    assert [c.run_id for c in plan.candidates] == ["old-older-20260501"]


def test_running_status_run_is_never_selected(tmp_path):
    _make_run(tmp_path, "live-20260601", status="running", age_days=30, last_event="step-start")

    plan = plan_gc(tmp_path, keep_days=7, now=NOW)

    assert plan.candidates == []
    assert any("running" in reason for _, reason in plan.skipped)


def test_gate_pending_run_needs_human_and_is_protected(tmp_path):
    _make_run(tmp_path, "gate-20260601", status="running", age_days=30, last_event="gate-pending")

    protected = plan_gc(tmp_path, keep_days=7, now=NOW)
    assert protected.candidates == []
    assert any("needs-human" in reason for _, reason in protected.skipped)

    # Even a done+gate run is only reclaimable when needs-human is explicitly included.
    _make_run(tmp_path, "gate2-20260601", status="done", age_days=30, last_event="gate-pending")
    included = plan_gc(tmp_path, keep_days=7, now=NOW, include_needs_human=True)
    assert "gate2-20260601" in [c.run_id for c in included.candidates]


def test_locked_run_is_skipped(tmp_path):
    run_dir = _make_run(tmp_path, "held-20260601", age_days=30)

    with run_lock(run_dir):  # a walker holds the advisory lock
        plan = plan_gc(tmp_path, keep_days=7, now=NOW)

    assert plan.candidates == []
    assert any("locked" in reason for _, reason in plan.skipped)


def test_junk_dirs_are_skipped_not_fatal(tmp_path):
    _make_run(tmp_path, "good-20260601", age_days=30)
    (tmp_path / "no-run-json").mkdir()  # dir without run.json
    (tmp_path / "loose.txt").write_text("x")  # a file
    corrupt = tmp_path / "corrupt-20260601"
    corrupt.mkdir()
    (corrupt / "run.json").write_text("{not json")

    plan = plan_gc(tmp_path, keep_days=7, now=NOW)

    assert [c.run_id for c in plan.candidates] == ["good-20260601"]
    assert any("no-run-json" in name for name, _ in plan.skipped)


def test_missing_created_at_is_never_selected_under_keep_last_only(tmp_path):
    # CRITICAL repro: a run whose run.json lacks created_at can't be ranked by keep_last —
    # it must be skipped as unevaluable, never fall through to a full-delete candidate.
    _make_run(tmp_path, "ranked-a-20260601", pipeline="p", age_days=30)
    _make_run(tmp_path, "ranked-b-20260702", pipeline="p", age_days=1)
    broken = tmp_path / "no-created-at-20260601"
    broken.mkdir()
    (broken / "run.json").write_text(
        json.dumps({"run_id": "no-created-at-20260601", "pipeline": "p", "status": "done"})
    )
    w = TrailWriter(broken, "no-created-at-20260601")
    w.emit("run-start")
    w.emit("run-done")
    w.close()

    plan = plan_gc(tmp_path, keep_last=2, now=NOW)

    assert "no-created-at-20260601" not in [c.run_id for c in plan.candidates]
    assert any(
        name == "no-created-at-20260601" and "created_at" in reason
        for name, reason in plan.skipped
    )


def test_invalid_created_at_is_never_selected_under_any_rule(tmp_path):
    bad = tmp_path / "bad-date-20260601"
    bad.mkdir()
    (bad / "run.json").write_text(
        json.dumps({"run_id": "bad-date-20260601", "pipeline": "p", "created_at": "not-a-date", "status": "done"})
    )
    w = TrailWriter(bad, "bad-date-20260601")
    w.emit("run-done")
    w.close()

    for kwargs in ({"keep_days": 7}, {"keep_last": 1}, {"keep_days": 7, "keep_last": 1}):
        plan = plan_gc(tmp_path, now=NOW, **kwargs)
        assert plan.candidates == [], f"selected under {kwargs}"
        assert any("created_at" in reason for _, reason in plan.skipped)


def test_plan_gc_is_pure_and_creates_no_lock_files(tmp_path):
    # plan_gc must not leave a .cairn.lock behind in run dirs that never had one —
    # a dry-run that mutates every run dir it probes isn't a dry-run.
    run_dir = _make_run(tmp_path, "old-20260601", age_days=30)
    assert not (run_dir / ".cairn.lock").exists()

    plan = plan_gc(tmp_path, keep_days=7, now=NOW)

    assert [c.run_id for c in plan.candidates] == ["old-20260601"]
    assert not (run_dir / ".cairn.lock").exists()


def test_keep_last_counts_a_live_run_toward_m(tmp_path):
    # The newest run is in flight; keep_last=2 still counts it toward M, so only the
    # oldest of the three is reclaimed (the live one is additionally protected anyway).
    _make_run(tmp_path, "p-oldest-20260601", pipeline="p", age_days=30)
    _make_run(tmp_path, "p-middle-20260615", pipeline="p", age_days=15)
    _make_run(tmp_path, "p-live-20260702", pipeline="p", status="running", age_days=1, last_event="step-start")

    plan = plan_gc(tmp_path, keep_last=2, now=NOW)

    assert [c.run_id for c in plan.candidates] == ["p-oldest-20260601"]
    assert any("running" in reason for _, reason in plan.skipped)


def test_naive_created_at_is_read_as_utc_not_a_crash(tmp_path):
    # Real run.json files carry a NAIVE created_at (walk.py writes datetime.now().isoformat());
    # plan_gc's aware `now` must not TypeError on them — a naive created_at reads as UTC and
    # is age-evaluated under keep_days and ranked under keep_last like any other run.
    for run_id, created_at in (
        ("naive-old-20260601", "2026-06-01T12:00:00"),
        ("naive-fresh-20260702", "2026-07-02T12:00:00"),
    ):
        run_dir = tmp_path / run_id
        run_dir.mkdir()
        (run_dir / "run.json").write_text(
            json.dumps({"run_id": run_id, "pipeline": "p", "created_at": created_at, "status": "done"})
        )
        w = TrailWriter(run_dir, run_id)
        w.emit("run-done")
        w.close()

    # keep_days: the old naive run is age-evaluated (~32 days) and selected; the fresh kept.
    by_days = plan_gc(tmp_path, keep_days=7, now=NOW)
    assert [c.run_id for c in by_days.candidates] == ["naive-old-20260601"]
    assert by_days.candidates[0].age_days is not None and by_days.candidates[0].age_days > 30

    # keep_last: both rank normally — newest-1 keeps the fresh run, selects the old one.
    by_last = plan_gc(tmp_path, keep_last=1, now=NOW)
    assert [c.run_id for c in by_last.candidates] == ["naive-old-20260601"]


def test_no_selection_rule_selects_nothing(tmp_path):
    _make_run(tmp_path, "old-20260601", age_days=99)
    plan = plan_gc(tmp_path, now=NOW)
    assert plan.candidates == []


def test_apply_full_delete_removes_the_run_dir(tmp_path):
    _make_run(tmp_path, "old-20260601", age_days=30)
    _make_run(tmp_path, "fresh-20260703", age_days=1)

    plan = plan_gc(tmp_path, keep_days=7, now=NOW)
    result = apply_gc(plan)

    assert isinstance(result, GcResult)
    assert result.deleted == ["old-20260601"]
    assert result.freed_bytes > 0
    assert not (tmp_path / "old-20260601").exists()
    assert (tmp_path / "fresh-20260703").exists()  # untouched


def test_apply_artifacts_only_preserves_the_audit_skeleton(tmp_path):
    run_dir = _make_run(tmp_path, "old-20260601", age_days=30)

    plan = plan_gc(tmp_path, keep_days=7, artifacts_only=True, now=NOW)
    result = apply_gc(plan)

    assert result.deleted == ["old-20260601"]
    # Run stays legible: run.json + trail.jsonl survive; heavyweight payloads are gone.
    assert (run_dir / "run.json").exists()
    assert (run_dir / "trail.jsonl").exists()
    assert not (run_dir / "logs").exists()
    assert not (run_dir / "artifacts").exists()


def test_apply_skips_a_run_locked_since_planning(tmp_path):
    run_dir = _make_run(tmp_path, "old-20260601", age_days=30)
    plan = plan_gc(tmp_path, keep_days=7, now=NOW)

    # A walker resumes the run between plan and apply — apply must not delete it.
    with run_lock(run_dir):
        result = apply_gc(plan)

    assert result.deleted == []
    assert any("locked" in reason for _, reason in result.errors)
    assert run_dir.exists()


def test_apply_refuses_a_candidate_outside_the_runs_root(tmp_path):
    outside = tmp_path / "outside"
    _make_run(outside, "escapee-20260601", age_days=30)
    real_root = tmp_path / "runs"
    real_root.mkdir()

    # Hand-forge a plan whose candidate dir lives outside the declared runs root.
    from cairn.kernel.gckit import GcCandidate

    plan = GcPlan(
        runs_root=real_root,
        candidates=[
            GcCandidate(
                run_id="escapee-20260601",
                run_dir=outside / "escapee-20260601",
                pipeline="p",
                status="done",
                created_at=None,
                age_days=None,
                reason="keep-days",
                artifacts_only=False,
            )
        ],
        skipped=[],
        artifacts_only=False,
        keep_days=7,
        keep_last=None,
    )
    result = apply_gc(plan)

    assert result.deleted == []
    assert any("outside" in reason for _, reason in result.errors)
    assert (outside / "escapee-20260601").exists()  # not touched


# --------------------------------------------------------------------------- #
# W1d — waiting-class outcome protection + reciprocal queue pins
# --------------------------------------------------------------------------- #


def _make_halted_run(
    runs_root,
    run_id,
    *,
    exit_code: int,
    age_days=30.0,
    pipeline="brease-rebuild",
    status="halted",
    artifacts=True,
):
    """Run whose trail ends in run-halt with the given exit_code (derived=halted)."""
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    created = NOW - timedelta(days=age_days)
    run_dir.joinpath("run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "pipeline": pipeline,
                "created_at": _iso(created),
                "status": status,
            }
        )
    )
    w = TrailWriter(run_dir, run_id)
    w.emit("run-start")
    w.emit("run-halt", data={"exit_code": exit_code, "reason": "park"})
    w.close()
    if artifacts:
        (run_dir / "logs").mkdir(exist_ok=True)
        (run_dir / "logs" / "capture.log").write_text("x" * 1000)
        (run_dir / "artifacts").mkdir(exist_ok=True)
        (run_dir / "artifacts" / "site-map.json").write_text("{}")
    return run_dir


def test_waiting_halt_exit_6_survives_keep_days_0(tmp_path):
    """Headless park (exit 6) derives as halted — still gc-protected by WAITING outcome."""
    _make_halted_run(tmp_path, "park-nh-20260601", exit_code=6, age_days=30)

    plan = plan_gc(tmp_path, keep_days=0, now=NOW)

    assert plan.candidates == []
    assert any(
        rid == "park-nh-20260601" and "waiting (needs_human)" in reason
        for rid, reason in plan.skipped
    )


def test_capacity_and_blocked_survive_include_needs_human(tmp_path):
    """Exit 8/9 parks are not force-collectable via include_needs_human (unlike gate)."""
    _make_halted_run(tmp_path, "park-cap-20260601", exit_code=8, age_days=30)
    _make_halted_run(tmp_path, "park-blk-20260601", exit_code=9, age_days=30)

    plan = plan_gc(tmp_path, keep_days=0, now=NOW, include_needs_human=True)

    assert plan.candidates == []
    reasons = {rid: reason for rid, reason in plan.skipped}
    assert "waiting (capacity)" in reasons["park-cap-20260601"]
    assert "waiting (blocked)" in reasons["park-blk-20260601"]


def test_plain_gate_still_respects_include_needs_human(tmp_path):
    """Back-compat: derived gate + include_needs_human still selects for collection."""
    _make_run(tmp_path, "gate-20260601", status="done", age_days=30, last_event="gate-pending")

    protected = plan_gc(tmp_path, keep_days=0, now=NOW)
    assert "gate-20260601" not in [c.run_id for c in protected.candidates]
    assert any("needs-human" in r for _, r in protected.skipped)

    included = plan_gc(tmp_path, keep_days=0, now=NOW, include_needs_human=True)
    assert "gate-20260601" in [c.run_id for c in included.candidates]


def test_done_run_not_protected_by_waiting_rule(tmp_path):
    """DONE (run-done / halt exit 0) is collectable — protection is waiting-class only."""
    _make_run(tmp_path, "done-20260601", age_days=30, last_event="run-done")
    # halt with exit 0 is DONE class, not WAITING
    _make_halted_run(tmp_path, "halt0-20260601", exit_code=0, age_days=30)

    plan = plan_gc(tmp_path, keep_days=0, now=NOW)

    ids = [c.run_id for c in plan.candidates]
    assert "done-20260601" in ids
    assert "halt0-20260601" in ids


def test_failed_halt_not_protected_by_waiting_rule(tmp_path):
    """Executor failure (exit 4) is FAILED class — collectable."""
    _make_halted_run(tmp_path, "fail-20260601", exit_code=4, age_days=30)

    plan = plan_gc(tmp_path, keep_days=0, now=NOW)
    assert "fail-20260601" in [c.run_id for c in plan.candidates]


def test_queue_pin_protects_full_delete_and_artifacts_only(tmp_path):
    from cairn.kernel.gckit import QUEUE_PIN_NAME, write_queue_pin

    run_dir = _make_run(tmp_path, "pinned-20260601", age_days=30)
    write_queue_pin(
        run_dir,
        trigger="on-webhook",
        item="p1-src-id-r1.json",
        pinned_at="2026-06-01T00:00:00.000Z",
    )
    assert (run_dir / QUEUE_PIN_NAME).is_file()

    full = plan_gc(tmp_path, keep_days=0, now=NOW)
    assert full.candidates == []
    assert any(
        rid == "pinned-20260601" and "queue-pinned (on-webhook)" in reason
        for rid, reason in full.skipped
    )

    slim = plan_gc(tmp_path, keep_days=0, artifacts_only=True, now=NOW)
    assert slim.candidates == []
    assert any("queue-pinned" in reason for _, reason in slim.skipped)


def test_apply_refuses_artifacts_only_on_pinned_run(tmp_path):
    """Even a hand-forged plan cannot slim a pin-bearing run (evidence is the pin)."""
    from cairn.kernel.gckit import GcCandidate, write_queue_pin

    run_dir = _make_run(tmp_path, "pinned-20260601", age_days=30)
    write_queue_pin(
        run_dir,
        trigger="t",
        item="i.json",
        pinned_at="2026-06-01T00:00:00.000Z",
    )
    plan = GcPlan(
        runs_root=tmp_path,
        candidates=[
            GcCandidate(
                run_id="pinned-20260601",
                run_dir=run_dir,
                pipeline="p",
                status="done",
                created_at=None,
                age_days=None,
                reason="keep-days",
                artifacts_only=True,
            )
        ],
        skipped=[],
        artifacts_only=True,
        keep_days=0,
        keep_last=None,
    )
    result = apply_gc(plan)
    assert result.deleted == []
    assert any("queue-pinned" in reason for _, reason in result.errors)
    assert (run_dir / "logs").exists()  # not slimmed


def test_stale_pin_still_protects(tmp_path):
    """Crash between terminal ledger placement and pin clear → over-protects (safe)."""
    from cairn.kernel.gckit import write_queue_pin

    # DONE trail but pin left behind (retire crashed before clear).
    run_dir = _make_run(tmp_path, "stale-pin-20260601", age_days=30, last_event="run-done")
    write_queue_pin(
        run_dir,
        trigger="t",
        item="left.json",
        pinned_at="2026-06-01T00:00:00.000Z",
    )

    plan = plan_gc(tmp_path, keep_days=0, now=NOW)
    assert plan.candidates == []
    assert any("queue-pinned" in reason for _, reason in plan.skipped)
