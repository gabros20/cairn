"""cairn learnings — aggregate `learn` events across every run into one ranked view.

The learning loop (docs/TOOLING-AND-GROWTH.md §7): agents record `learnings[]` on the STEP
sentinel → the walker emits them as `learn` trail events → this module scans all run dirs
under the runs root, collects those events, and renders them for a human to curate. Never
automatic promotion; this is the *aggregate* rung only.

The `learn` envelope the walker writes is `{event: "learn", node, cycle, data: {note, tag}}`
(cairn/kernel/walk.py) — this module reads exactly that shape via trail.read_trail, tolerating
missing/corrupt trails and non-run directories in the runs root.

stdlib only. No servers, no state — a pure read over the on-disk trail contract.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cairn.kernel.runstate import RUN_JSON
from cairn.kernel.trail import TRAIL_NAME, read_trail


@dataclass(frozen=True)
class Learning:
    """One aggregated `learn` event, tagged with the run it came from.

    `note`/`tag` are the walker's learn payload; `pipeline` is best-effort from the run's
    run.json (None when it's missing/unreadable — the learning is still legible).
    """

    run_id: str
    pipeline: str | None
    node: str | None
    at: str
    tag: str | None
    note: str | None
    cycle: int | None
    seq: int


def _parse_at(at: str) -> datetime | None:
    """Parse an envelope `at` (…Z) to an aware datetime; None if unparseable."""
    try:
        return datetime.fromisoformat(at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _parse_since(since: str) -> datetime:
    """Parse a --since value into an inclusive UTC lower bound.

    Accepts a bare ISO date (`2026-07-03` → midnight UTC that day) or a full ISO datetime.
    Raises ValueError on anything else, so the CLI can report a precise message.
    """
    dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _pipeline_of(run_dir: Path) -> str | None:
    """Best-effort pipeline name from run.json; None if absent/corrupt."""
    try:
        doc = json.loads((run_dir / RUN_JSON).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pipeline = doc.get("pipeline") if isinstance(doc, dict) else None
    return pipeline if isinstance(pipeline, str) else None


def collect_learnings(
    runs_root: Path,
    *,
    since: str | None = None,
    tag: str | None = None,
    warnings: list[str] | None = None,
) -> list[Learning]:
    """Aggregate `learn` events from every run under `runs_root`, filtered and sorted.

    Filters: `since` (inclusive ISO date/datetime lower bound on the event `at`) and `tag`
    (exact match). Ordering is deterministic — by `at`, then `run_id`, then `seq`.

    Robustness: directories with no trail, unreadable trails, and non-run junk are skipped,
    never fatal; if `warnings` is given, one human-readable line per skip is appended to it.
    """
    root = Path(runs_root)
    since_dt = _parse_since(since) if since else None

    out: list[Learning] = []
    if not root.is_dir():
        if warnings is not None:
            warnings.append(f"runs root does not exist: {root}")
        return out

    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir():
            continue
        if not (run_dir / TRAIL_NAME).exists():
            if warnings is not None:
                warnings.append(f"skipped {run_dir.name}: no {TRAIL_NAME}")
            continue

        pipeline = _pipeline_of(run_dir)
        try:
            events = list(read_trail(run_dir))
        except OSError:
            if warnings is not None:
                warnings.append(f"skipped {run_dir.name}: unreadable trail")
            continue

        for ev in events:
            if ev.get("event") != "learn":
                continue
            data = ev.get("data") or {}
            ev_tag = data.get("tag")
            if tag is not None and ev_tag != tag:
                continue
            at = ev.get("at", "")
            if since_dt is not None:
                at_dt = _parse_at(at)
                if at_dt is None or at_dt < since_dt:
                    continue
            out.append(
                Learning(
                    run_id=ev.get("run_id") or run_dir.name,
                    pipeline=pipeline,
                    node=ev.get("node"),
                    at=at,
                    tag=ev_tag,
                    note=data.get("note"),
                    cycle=ev.get("cycle"),
                    seq=ev.get("seq", 0),
                )
            )

    out.sort(key=lambda x: (x.at, x.run_id, x.seq))
    return out


def render_learnings(learnings: Iterable[Learning]) -> str:
    """Render learnings as a deterministic, human-readable block (the CLI's default view).

    One line per learning: `<at>  <pipeline>/<node>  [tag]  note`. A trailing count summary
    lets an operator see breadth at a glance. Empty input renders a single 'no learnings' line.
    """
    items = list(learnings)
    if not items:
        return "no learnings found"

    lines = []
    for lg in items:
        where = "/".join(p for p in (lg.pipeline, lg.node) if p) or "?"
        tag = f"[{lg.tag}] " if lg.tag else ""
        note = lg.note if lg.note is not None else ""
        lines.append(f"{lg.at}  {where}  {tag}{note}".rstrip())

    tags = sorted({lg.tag for lg in items if lg.tag})
    summary = f"\n{len(items)} learning(s)"
    if tags:
        summary += f" across tags: {', '.join(tags)}"
    return "\n".join(lines) + summary
