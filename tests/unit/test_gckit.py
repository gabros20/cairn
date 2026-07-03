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
