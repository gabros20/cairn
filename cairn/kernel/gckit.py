"""cairn gc — explicit retention over the runs root. NEVER automatic (SECURITY.md §5).

Runs are the audit record; deleting one is an operator's decision, so this module splits
into a pure planner and an explicit applier:

  * `plan_gc(...)` selects deletion candidates by `--keep-days N` (delete runs older than N
    days) and/or `--keep-last M` (keep the newest M runs *per pipeline*). It is a dry-run —
    it deletes nothing — and it is the default the CLI shows.
  * `apply_gc(plan)` performs the deletion the plan describes, and only that.

Safety first:
  * A run that is live is never selected — `run.json.status == "running"`, a held advisory
    flock, or a trail whose derived status is `running`/`gate` all protect it. A `gate`
    (needs-human) run is protected unless `include_needs_human=True`.
  * A run with a missing/corrupt run.json or an unparseable `created_at` is treated as junk /
    unevaluable and skipped (never selected), so bad data can never cause a deletion.
  * `--artifacts-only` slims a run instead of deleting it: it drops heavyweight artifact/log
    payloads but PRESERVES the audit skeleton — `run.json`, `trail.jsonl`, `.cairn.lock` —
    so the run stays legible in `cairn ps` / `cairn trail` forever.

stdlib only.
"""

from __future__ import annotations

import fcntl
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from cairn.kernel.runstate import LOCK_NAME, RUN_JSON
from cairn.kernel.trail import TRAIL_NAME, derive_status

# The audit skeleton that survives an `--artifacts-only` slim — everything else in the run
# dir is heavyweight payload and may be dropped.
SURVIVES_ARTIFACTS_ONLY: frozenset[str] = frozenset({RUN_JSON, TRAIL_NAME, LOCK_NAME})

# Derived-status values that mean the run is alive / awaiting a human and must be protected.
_LIVE = {"running"}
_NEEDS_HUMAN = {"gate"}


@dataclass(frozen=True)
class GcCandidate:
    """One run selected for gc, with why it was picked and how it will be handled."""

    run_id: str
    run_dir: Path
    pipeline: str | None
    status: str            # derived status at plan time (done/halted/stale/…)
    created_at: str | None
    age_days: float | None
    reason: str            # "keep-days" | "keep-last" | "keep-days+keep-last"
    artifacts_only: bool   # slim (True) vs full delete (False)


@dataclass(frozen=True)
class GcPlan:
    """A dry-run gc preview: what apply_gc would delete, and what it skipped and why."""

    runs_root: Path
    candidates: list[GcCandidate]
    skipped: list[tuple[str, str]]  # (run_id / dir name, reason)
    artifacts_only: bool
    keep_days: int | None
    keep_last: int | None


@dataclass(frozen=True)
class GcResult:
    """The outcome of applying a plan."""

    deleted: list[str]                    # run_ids fully removed or slimmed
    freed_bytes: int
    errors: list[tuple[str, str]] = field(default_factory=list)  # (run_id, reason)


@dataclass
class _RunInfo:
    run_id: str
    run_dir: Path
    pipeline: str | None
    created_at: str | None
    created_dt: datetime | None
    status_runjson: str | None
    derived: str


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _load_info(run_dir: Path) -> _RunInfo | None:
    """Gather what gc needs about a run dir; None if it isn't a legible run."""
    try:
        doc = json.loads((run_dir / RUN_JSON).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    created_at = doc.get("created_at")
    pipeline = doc.get("pipeline")
    return _RunInfo(
        run_id=doc.get("run_id") or run_dir.name,
        run_dir=run_dir,
        pipeline=pipeline if isinstance(pipeline, str) else None,
        created_at=created_at if isinstance(created_at, str) else None,
        created_dt=_parse_dt(created_at) if isinstance(created_at, str) else None,
        status_runjson=doc.get("status") if isinstance(doc.get("status"), str) else None,
        derived=derive_status(run_dir).status,
    )


def _lock_free(run_dir: Path) -> bool:
    """True if no walker holds the run's advisory lock right now — WITHOUT touching the dir.

    An absent lockfile means nothing holds it (runstate.run_lock creates the file when it
    takes the lock). When one exists it is opened read-only and flock-probed non-blocking;
    the probe never creates, truncates, or writes the lockfile, keeping plan_gc a true
    dry-run.
    """
    lock_path = run_dir / LOCK_NAME
    if not lock_path.exists():
        return True
    try:
        fh = lock_path.open("r", encoding="utf-8")
    except OSError:
        return False  # can't even open it — treat as held, never as reclaimable
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return True
    finally:
        fh.close()


def plan_gc(
    runs_root: Path,
    *,
    keep_days: int | None = None,
    keep_last: int | None = None,
    artifacts_only: bool = False,
    now: datetime,
    include_needs_human: bool = False,
) -> GcPlan:
    """Select deletion candidates under `runs_root` — a pure dry-run (deletes nothing).

    `keep_days` retains runs younger than N days; `keep_last` retains the newest M runs per
    pipeline. With both, a run survives if *either* rule retains it (deleted only when it is
    both too old AND not among the newest-M of its pipeline). With neither, nothing is
    selected. `now` is injected (never `datetime.now()`) so selection is deterministic.

    keep_last ranks *all* legible runs of a pipeline — in-flight (running/locked/gate) runs
    count toward M, so "keep the newest 2" means the 2 newest runs, not the 2 newest already
    reclaimable ones. Runs without a parseable `created_at` are unevaluable by either rule
    and are skipped (reported in `skipped`), never selected.
    """
    root = Path(runs_root)
    skipped: list[tuple[str, str]] = []
    infos: list[_RunInfo] = []

    if root.is_dir():
        for run_dir in sorted(root.iterdir()):
            if not run_dir.is_dir():
                continue
            info = _load_info(run_dir)
            if info is None:
                skipped.append((run_dir.name, "not a legible run (missing/corrupt run.json)"))
                continue
            infos.append(info)

    # Rank the newest-M per pipeline across *all* legible runs with a valid created_at, so
    # keep_last protects the M newest even if one of them happens to be protected for liveness.
    kept_by_last: set[str] = set()
    if keep_last is not None:
        by_pipeline: dict[str | None, list[_RunInfo]] = {}
        for info in infos:
            if info.created_dt is not None:
                by_pipeline.setdefault(info.pipeline, []).append(info)
        for group in by_pipeline.values():
            group.sort(key=lambda i: (i.created_dt, i.run_id), reverse=True)
            for info in group[:keep_last]:
                kept_by_last.add(info.run_dir.as_posix())

    candidates: list[GcCandidate] = []
    for info in infos:
        # --- liveness / safety protection (never delete an active or waiting run) --------
        # A gate is a distinct state (awaiting a human), classified before liveness so a
        # gate-pending run reads as needs-human even while its run.json still says running.
        if info.derived in _NEEDS_HUMAN:
            if not include_needs_human:
                skipped.append((info.run_id, "protected: needs-human (gate)"))
                continue
        elif info.status_runjson == "running" or info.derived in _LIVE:
            skipped.append((info.run_id, "protected: run is running"))
            continue
        if not _lock_free(info.run_dir):
            skipped.append((info.run_id, "protected: run is locked"))
            continue

        # --- selection rules (union of retention) ----------------------------------------
        if keep_days is None and keep_last is None:
            continue  # no rule → nothing selected

        # Both rules reason from created_at (age for keep_days, newest-M rank for keep_last).
        # A run without a parseable created_at can't be evaluated by EITHER — it is skipped,
        # never selected, so bad data can never cause a deletion.
        if info.created_dt is None:
            skipped.append((info.run_id, "unevaluable: missing/invalid created_at"))
            continue

        age_days = (now - info.created_dt).total_seconds() / 86400.0
        old_enough = keep_days is not None and info.created_dt < (now - timedelta(days=keep_days))

        retained_by_days = keep_days is not None and not old_enough
        retained_by_last = keep_last is not None and info.run_dir.as_posix() in kept_by_last
        if retained_by_days or retained_by_last:
            continue

        reasons = []
        if keep_days is not None:
            reasons.append("keep-days")
        if keep_last is not None and info.run_dir.as_posix() not in kept_by_last:
            reasons.append("keep-last")
        candidates.append(
            GcCandidate(
                run_id=info.run_id,
                run_dir=info.run_dir,
                pipeline=info.pipeline,
                status=info.derived,
                created_at=info.created_at,
                age_days=age_days,
                reason="+".join(reasons) or "selected",
                artifacts_only=artifacts_only,
            )
        )

    candidates.sort(key=lambda c: c.run_id)
    return GcPlan(
        runs_root=root,
        candidates=candidates,
        skipped=skipped,
        artifacts_only=artifacts_only,
        keep_days=keep_days,
        keep_last=keep_last,
    )


def _dir_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _slim(run_dir: Path) -> int:
    """Drop every top-level entry except the audit skeleton; return bytes freed."""
    freed = 0
    for entry in run_dir.iterdir():
        if entry.name in SURVIVES_ARTIFACTS_ONLY:
            continue
        freed += _dir_bytes(entry) if entry.is_dir() else _stat_size(entry)
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    return freed


def _stat_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def apply_gc(plan: GcPlan) -> GcResult:
    """Execute a plan's deletions — the only path that removes anything.

    Each candidate is re-checked for its advisory lock at apply time (a run may have been
    resumed since planning); a now-locked run is skipped as an error, never force-deleted.
    The lock is not held across the deletion itself, so a walker starting in the window
    between the re-check and the rmtree is an inherent (accepted) race — gc is an explicit
    operator action on runs already judged dead, not a concurrent-safe primitive.
    Full delete removes the run dir; `artifacts_only` slims it to the audit skeleton. A
    candidate whose dir escaped `plan.runs_root` is refused — apply never deletes outside root.
    """
    deleted: list[str] = []
    errors: list[tuple[str, str]] = []
    freed = 0
    root = Path(plan.runs_root).resolve()

    for cand in plan.candidates:
        run_dir = cand.run_dir
        try:
            resolved = run_dir.resolve()
        except OSError:
            errors.append((cand.run_id, "path could not be resolved"))
            continue
        if resolved != root and root not in resolved.parents:
            errors.append((cand.run_id, "refused: run dir is outside the runs root"))
            continue
        if not run_dir.exists():
            errors.append((cand.run_id, "run dir no longer exists"))
            continue
        if not _lock_free(run_dir):
            errors.append((cand.run_id, "skipped: run became locked since planning"))
            continue

        try:
            if cand.artifacts_only:
                freed += _slim(run_dir)
            else:
                freed += _dir_bytes(run_dir)
                shutil.rmtree(run_dir)
            deleted.append(cand.run_id)
        except OSError as exc:
            errors.append((cand.run_id, f"delete failed: {exc}"))

    return GcResult(deleted=deleted, freed_bytes=freed, errors=errors)
