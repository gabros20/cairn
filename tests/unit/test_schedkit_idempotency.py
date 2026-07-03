"""Idempotency: the pure predicate that makes a scheduled `cairn run --idempotent`
a no-op (or a resume) when an equivalent successful run already exists (SCHEDULING.md §3).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from cairn.kernel.schedkit import find_idempotent_run, idempotency_key

NOON = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
NEXT_DAY = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


def _write_run(runs_root: Path, run_id: str, *, pipeline, params, status, created_at) -> Path:
    d = runs_root / run_id
    d.mkdir(parents=True)
    doc = {
        "run_id": run_id,
        "pipeline": pipeline,
        "pipeline_hash": "sha256:x",
        "cairn_version": "0.1.0",
        "params": params,
        "dims": {},
        "executors": {"default": "stub"},
        "models": {},
        "created_at": created_at,
        "status": status,
        "nodes": {},
    }
    (d / "run.json").write_text(json.dumps(doc), encoding="utf-8")
    return d


def test_key_is_stable_and_date_bucketed():
    k1 = idempotency_key(pipeline="p", params={"url": "https://a"}, now=NOON)
    k2 = idempotency_key(pipeline="p", params={"url": "https://a"}, now=NOON)
    assert k1 == k2
    # same params, next day → different key (run_id embeds {date})
    assert idempotency_key(pipeline="p", params={"url": "https://a"}, now=NEXT_DAY) != k1


def test_key_changes_with_params_and_pipeline():
    base = idempotency_key(pipeline="p", params={"url": "https://a"}, now=NOON)
    assert idempotency_key(pipeline="p", params={"url": "https://b"}, now=NOON) != base
    assert idempotency_key(pipeline="q", params={"url": "https://a"}, now=NOON) != base


def test_key_ignores_param_ordering():
    a = idempotency_key(pipeline="p", params={"url": "u", "mode": "m"}, now=NOON)
    b = idempotency_key(pipeline="p", params={"mode": "m", "url": "u"}, now=NOON)
    assert a == b


def test_find_returns_completed_run_as_a_skip(tmp_path):
    _write_run(
        tmp_path, "acme-20260703",
        pipeline="p", params={"url": "https://a"}, status="done",
        created_at="2026-07-03T09:00:00.000Z",
    )
    match = find_idempotent_run(tmp_path, pipeline="p", params={"url": "https://a"}, now=NOON)
    assert match is not None
    assert match.run_id == "acme-20260703"
    assert match.complete is True


def test_find_returns_incomplete_run_as_a_resume(tmp_path):
    _write_run(
        tmp_path, "acme-20260703",
        pipeline="p", params={"url": "https://a"}, status="halted",
        created_at="2026-07-03T09:00:00.000Z",
    )
    match = find_idempotent_run(tmp_path, pipeline="p", params={"url": "https://a"}, now=NOON)
    assert match is not None
    assert match.complete is False  # caller resumes rather than creating a variant


def test_find_no_match_when_params_differ(tmp_path):
    _write_run(
        tmp_path, "acme-20260703",
        pipeline="p", params={"url": "https://a"}, status="done",
        created_at="2026-07-03T09:00:00.000Z",
    )
    assert find_idempotent_run(tmp_path, pipeline="p", params={"url": "https://b"}, now=NOON) is None


def test_find_no_match_across_days(tmp_path):
    # a run completed yesterday does NOT satisfy today's firing
    _write_run(
        tmp_path, "acme-20260703",
        pipeline="p", params={"url": "https://a"}, status="done",
        created_at="2026-07-03T09:00:00.000Z",
    )
    assert find_idempotent_run(tmp_path, pipeline="p", params={"url": "https://a"}, now=NEXT_DAY) is None


def test_find_missing_runs_root_is_none(tmp_path):
    assert find_idempotent_run(tmp_path / "nope", pipeline="p", params={}, now=NOON) is None
