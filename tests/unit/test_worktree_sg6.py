"""W8-T2 — worktree doctrine + SG6 audit quarantine.

Covers: run meta worktree field, gc prune via Runner, linked-worktree lock
scoping (also in test_resource_locks), audit quarantine of settled violations
(not in-grace), D7 (no worktree + clean ledger unchanged). Seams only — no
os.* monkeypatch of production paths beyond utime for mtime grace.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cairn.kernel.gckit import apply_gc, plan_gc, prune_run_worktree
from cairn.kernel.proc import RunResult, RunnerBase
from cairn.kernel.queue_ledger import (
    AUDIT_GRACE_S,
    audit_and_quarantine,
    audit_ledger,
    count_quarantined,
    quarantine_dir,
    release_quarantine,
    write_pointer,
)
from cairn.kernel.runstate import record_worktree, read_worktree
from cairn.kernel.trail import TrailWriter


NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Fake Runner for worktree remove
# --------------------------------------------------------------------------- #


class _WorktreeRunner(RunnerBase):
    """Records git worktree remove calls; configurable return codes."""

    def __init__(self, *, fail_paths: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_paths = fail_paths or set()

    def spawn(self, argv, *, input=None, cwd=None):  # pragma: no cover
        raise NotImplementedError

    def run(self, argv, *, input=None, cwd=None) -> RunResult:
        self.calls.append(list(argv))
        # git -C <path> worktree remove --force <path>
        if (
            len(argv) >= 6
            and argv[0] == "git"
            and argv[1] == "-C"
            and argv[3:5] == ["worktree", "remove"]
        ):
            target = argv[-1]
            if target in self.fail_paths:
                return RunResult(returncode=128, stdout="", stderr="fatal: not a working tree")
            return RunResult(returncode=0, stdout="", stderr="")
        return RunResult(returncode=1, stdout="", stderr="unexpected")


def _make_run(
    runs_root: Path,
    run_id: str,
    *,
    status: str = "done",
    age_days: float = 30.0,
    worktree: str | None = None,
) -> Path:
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    created = NOW - timedelta(days=age_days)
    doc = {
        "run_id": run_id,
        "pipeline": "fix-demo",
        "created_at": _iso(created),
        "status": status,
    }
    if worktree is not None:
        doc["worktree"] = worktree
    (run_dir / "run.json").write_text(json.dumps(doc), encoding="utf-8")
    w = TrailWriter(run_dir, run_id)
    w.emit("run-start")
    w.emit("run-done")
    w.close()
    return run_dir


# --------------------------------------------------------------------------- #
# Worktree meta
# --------------------------------------------------------------------------- #


def test_record_and_read_worktree(tmp_path: Path) -> None:
    """record_worktree writes optional field; clear removes it (D7 absent)."""
    # Full schema-valid run.json for record_worktree (update_run validates).
    run_dir = tmp_path / "r1"
    run_dir.mkdir()
    payload = {
        "run_id": "r1",
        "pipeline": "p",
        "pipeline_hash": "h",
        "cairn_version": "0.0.0",
        "params": {},
        "dims": {},
        "executors": {"default": "shell"},
        "models": {},
        "created_at": _iso(NOW),
        "status": "running",
        "nodes": {},
    }
    (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")

    assert read_worktree(run_dir) is None
    wt = tmp_path / "wt-item-a"
    record_worktree(run_dir, wt)
    assert read_worktree(run_dir) == str(wt)
    doc = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert doc["worktree"] == str(wt)

    record_worktree(run_dir, None)
    assert read_worktree(run_dir) is None
    doc = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert "worktree" not in doc


# --------------------------------------------------------------------------- #
# gc prunes worktree
# --------------------------------------------------------------------------- #


def test_apply_gc_prunes_recorded_worktree(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    wt = tmp_path / "wt-r1"
    wt.mkdir()
    run_dir = _make_run(runs_root, "old-run", worktree=str(wt))
    assert run_dir.is_dir()

    runner = _WorktreeRunner()
    plan = plan_gc(runs_root, keep_days=7, now=NOW)
    assert any(c.run_id == "old-run" for c in plan.candidates)
    result = apply_gc(plan, runner=runner)
    assert "old-run" in result.deleted
    assert not run_dir.exists()
    # Exactly one worktree remove for the recorded path.
    removes = [c for c in runner.calls if "worktree" in c and "remove" in c]
    assert len(removes) == 1
    assert str(wt) in removes[0]


def test_apply_gc_no_worktree_is_noop_on_git(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    _make_run(runs_root, "old-run")  # no worktree field
    runner = _WorktreeRunner()
    plan = plan_gc(runs_root, keep_days=7, now=NOW)
    result = apply_gc(plan, runner=runner)
    assert "old-run" in result.deleted
    assert runner.calls == []  # never invoked git


def test_prune_already_gone_worktree_is_clean(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "r", worktree=str(tmp_path / "missing-wt"))
    runner = _WorktreeRunner()
    # Path does not exist → no git call, clean diagnostic.
    note = prune_run_worktree(run_dir, runner=runner)
    assert note is not None
    assert "already gone" in note
    assert runner.calls == []


def test_prune_tolerates_git_not_a_working_tree(tmp_path: Path) -> None:
    wt = tmp_path / "half-broken"
    wt.mkdir()
    run_dir = _make_run(tmp_path, "r", worktree=str(wt))
    runner = _WorktreeRunner(fail_paths={str(wt)})
    note = prune_run_worktree(run_dir, runner=runner)
    assert note is not None
    assert "already gone" in note or "prune failed" in note


# --------------------------------------------------------------------------- #
# SG6 audit quarantine
# --------------------------------------------------------------------------- #


def test_audit_quarantine_settled_violation(tmp_path: Path) -> None:
    """Settled item-without-pointer → moved to .quarantine/ + issue note."""
    watch = tmp_path / "inbox"
    claim = watch / ".claim"
    claim.mkdir(parents=True)
    item = claim / "lone.json"
    item.write_text("{}", encoding="utf-8")
    old = 1_000_000.0
    os.utime(item, (old, old))
    now = old + AUDIT_GRACE_S + 10

    # Pure audit still sees the issue.
    issues = audit_ledger(watch, now=now)
    assert any("item without pointer" in i for i in issues)

    diags = audit_and_quarantine(watch, now=now)
    assert any("item without pointer" in d for d in diags)
    assert any("quarantined" in d for d in diags)
    assert not item.exists()  # left live lane
    q = quarantine_dir(watch)
    assert (q / "lone.json").is_file()
    assert (q / "lone.json.issue").is_file()
    assert "item without pointer" in (q / "lone.json.issue").read_text(encoding="utf-8")
    assert count_quarantined(watch) == 1
    # Second pass: clean (already quarantined).
    assert audit_ledger(watch, now=now) == []
    assert audit_and_quarantine(watch, now=now) == []


def test_audit_clean_ledger_no_quarantine(tmp_path: Path) -> None:
    watch = tmp_path / "inbox"
    watch.mkdir()
    assert audit_and_quarantine(watch) == []
    assert not quarantine_dir(watch).exists()
    assert count_quarantined(watch) == 0


def test_audit_in_grace_claim_not_quarantined(tmp_path: Path) -> None:
    """Transient in-grace claim (fresh mtime) is left alone — not a settled violation."""
    watch = tmp_path / "inbox"
    claim = watch / ".claim"
    claim.mkdir(parents=True)
    item = claim / "fresh.json"
    item.write_text("{}", encoding="utf-8")
    # mtime = now → within AUDIT_GRACE_S.
    now = 2_000_000.0
    os.utime(item, (now, now))
    issues = audit_ledger(watch, now=now)
    assert issues == []
    diags = audit_and_quarantine(watch, now=now)
    assert diags == []
    assert item.is_file()  # still in .claim/
    assert count_quarantined(watch) == 0


def test_audit_identity_two_states_quarantines_both(tmp_path: Path) -> None:
    watch = tmp_path / "inbox"
    name_a = "p1-jira-abc-r10.json"
    name_b = "p2-jira-abc-r11.json"
    for lane, name in ((".claim", name_a), (".waiting", name_b)):
        d = watch / lane
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("{}", encoding="utf-8")
        write_pointer(d / ".runs" / name, run_dir="/runs/x")
    (watch / ".claim" / ".ids").mkdir(parents=True)
    (watch / ".claim" / ".ids" / "jira-abc").write_text("", encoding="utf-8")

    diags = audit_and_quarantine(watch)
    assert any("identity in two live states" in d for d in diags)
    assert not (watch / ".claim" / name_a).exists()
    assert not (watch / ".waiting" / name_b).exists()
    assert count_quarantined(watch) >= 2
    # Drain would not process them — not in live lanes.
    assert audit_ledger(watch) == [] or not any(
        "two live states" in i for i in audit_ledger(watch)
    )


def test_audit_never_auto_deletes(tmp_path: Path) -> None:
    """Quarantine preserves evidence — content still readable under .quarantine/."""
    watch = tmp_path / "inbox"
    claim = watch / ".claim"
    claim.mkdir(parents=True)
    body = '{"evidence": true}\n'
    item = claim / "keep-me.json"
    item.write_text(body, encoding="utf-8")
    old = 1_000_000.0
    os.utime(item, (old, old))
    audit_and_quarantine(watch, now=old + AUDIT_GRACE_S + 1)
    q_item = quarantine_dir(watch) / "keep-me.json"
    assert q_item.read_text(encoding="utf-8") == body


def test_release_quarantine_returns_to_inbox(tmp_path: Path) -> None:
    """Operator release path: .quarantine/ → inbox for re-admission (M3)."""
    watch = tmp_path / "inbox"
    claim = watch / ".claim"
    claim.mkdir(parents=True)
    body = '{"id": "x"}\n'
    item = claim / "p1-jira-abc-r10.json"
    item.write_text(body, encoding="utf-8")
    old = 1_000_000.0
    os.utime(item, (old, old))
    audit_and_quarantine(watch, now=old + AUDIT_GRACE_S + 1)
    assert count_quarantined(watch) == 1
    assert not item.exists()

    diags = release_quarantine(watch, "p1-jira-abc-r10.json")
    assert any("released quarantine" in d for d in diags)
    assert count_quarantined(watch) == 0
    inbox_item = watch / "p1-jira-abc-r10.json"
    assert inbox_item.is_file()
    assert inbox_item.read_text(encoding="utf-8") == body
    # issue sidecar gone
    assert not (quarantine_dir(watch) / "p1-jira-abc-r10.json.issue").exists()


def test_release_quarantine_clean_is_noop(tmp_path: Path) -> None:
    watch = tmp_path / "inbox"
    watch.mkdir()
    assert release_quarantine(watch) == []
    diags = release_quarantine(watch, "missing.json")
    assert len(diags) == 1
    assert "no quarantine entry" in diags[0]


def test_release_quarantine_all(tmp_path: Path) -> None:
    watch = tmp_path / "inbox"
    q = quarantine_dir(watch)
    q.mkdir(parents=True)
    (q / "a.json").write_text("{}", encoding="utf-8")
    (q / "a.json.issue").write_text("issue a\n", encoding="utf-8")
    (q / "b.json").write_text("{}", encoding="utf-8")
    diags = release_quarantine(watch)
    assert len([d for d in diags if "released" in d]) == 2
    assert count_quarantined(watch) == 0
    assert (watch / "a.json").is_file()
    assert (watch / "b.json").is_file()


# --------------------------------------------------------------------------- #
# Scaffold pattern surface (worktree: true + deliver locks)
# --------------------------------------------------------------------------- #


def test_fix_scaffold_declares_worktree_pattern(tmp_path: Path) -> None:
    from cairn.kernel import newkit, sourcekit
    from cairn.kernel.plan import plan as build_plan

    ws = newkit.new_workspace("wtws", tmp_path)
    sourcekit.new_source("github-issues", ws)
    fix_yaml = (ws / "pipelines" / "fix-github-issues.yaml").read_text(encoding="utf-8")
    assert "worktree: true" in fix_yaml
    assert "locks:" in fix_yaml
    assert "repo:." in fix_yaml
    p = build_plan(
        ws,
        "fix-github-issues",
        {"event": "/tmp/item.json"},
        now=NOW,
    )
    assert p.worktree is True
    deliver = next(n for n in p.nodes if getattr(n, "id", None) == "deliver")
    assert "repo:." in deliver.locks
