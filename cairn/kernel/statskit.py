"""cairn stats — read-only factory gauge board over runs + trigger ledgers.

Pure projection (FACTORY-PLAN §10 / W7). Reads trail.jsonl, gates/*.json, and
ledger dirs; never creates, modifies, or deletes anything. Inject ``now`` for
all time math so tests are deterministic (no wall-clock in the computation core).

Output honesty: **window aggregates** (throughput, step/validator rates, retry,
gate latency, human-answered share, resolved blocked-time) are computed over the
scanned window; **snapshots** (currently_blocked, trigger depths / lease /
circuit) are point-in-time and must not be mixed into historical means.

Metric signals:

- **throughput** — ``run-done`` terminals in the window, by pipeline + origin.
  ``per_day`` is only reported when the effective window is ≥ 1 day (no sub-day
  ×24 inflation); otherwise the raw completed count is shown over the real span.
- **step_success_rate** — every ``step-done`` vs ``retry`` + terminal
  ``step-fail`` with ``validator_reasons``. This is step success, not validator
  health (many steps have no produces).
- **validator_pass_rate** — only validator-bearing outcomes: ``step-done`` whose
  ``data.artifacts`` is a non-empty list (a real produces check ran and passed)
  vs ``retry`` / ``step-fail`` carrying ``validator_reasons``. Bare steps with
  empty/missing artifacts do not enter this rate.
- **retry rate** — count of ``retry`` trail events, overall and per node.
- **gate latency** — first ``gate-pending.at`` → decision time, prefer
  ``gate-answered.at`` trail event, then gates/<name>.json ``at``, then mtime
  (last resort; mtime can be touched by copy/gc). Negative (clock skew) dropped.
- **waiting/blocked/capacity depth** — live snapshot via ``count_by_class``.
- **human-answered share (presence, not comprehension)** — over gates of
  *completed* runs only: ``by: tty|external`` (human) vs ``default|flag|lane:*``
  (machine). ``by`` is taken from the ``gate-answered`` trail event, else a
  MAC-verified gate file (forged/unsigned files do not skew the share).
- **blocked_time_resolved** — closed BLOCKED(9) parks only (halt → next trail
  event). Still-parked parks are a **snapshot** (``currently_blocked``), not
  summed into the resolved mean.
- **lease / circuit** — per-trigger live snapshot (historical reaps not durable).
"""

from __future__ import annotations

import hmac
import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cairn.kernel.gatekeys import compute_mac, load_run_key
from cairn.kernel.queue_ledger import count_by_class, lease_status, read_circuit
from cairn.kernel.runstate import RUN_JSON
from cairn.kernel.trail import TRAIL_NAME, read_trail
from cairn.kernel.types import ExitCode

GATE_DIR = "gates"
HUMAN_BY = frozenset({"tty", "external"})
MACHINE_BY_EXACT = frozenset({"default", "flag"})
HUMAN_ANSWERED_LABEL = "human-answered share (presence, not comprehension)"
# Denominator caveat (M4): only gates of completed (run-done) runs.
HUMAN_ANSWERED_SCOPE = "over gates of completed runs only"
SCHEMA_VERSION = 2
MIN_PER_DAY_WINDOW_DAYS = 1.0


def parse_at(at: str | None) -> datetime | None:
    """Parse a trail/gate ``at`` to aware UTC; None if missing/unparseable."""
    if not isinstance(at, str):
        return None
    try:
        dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_since(since: str) -> datetime:
    """Parse ``--since`` (ISO date or datetime) to inclusive UTC lower bound.

    Bare dates (``2026-07-03``) become midnight UTC that day. Raises ValueError
    on garbage so the CLI can map it to ExitCode.CONFIG.
    """
    dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _secs(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds())


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    """Nearest-rank percentile; ``p`` in 0..100. Empty → None."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = max(1, min(len(sorted_vals), int(math.ceil(p / 100.0 * len(sorted_vals)))))
    return sorted_vals[rank - 1]


def _is_human_by(by: str) -> bool:
    return by in HUMAN_BY


def _is_machine_by(by: str) -> bool:
    return by in MACHINE_BY_EXACT or by.startswith("lane:")


def _format_now(now: datetime) -> str:
    return (
        now.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def terminal_from_events(events: list[dict]) -> tuple[str, int | None]:
    """Last terminal from an already-materialised trail (avoids a second read)."""
    kind = "none"
    exit_code: int | None = None
    for ev in events:
        event = ev.get("event")
        if event == "run-done":
            kind = "done"
            exit_code = None
        elif event == "run-halt":
            kind = "halt"
            data = ev.get("data") or {}
            raw = data.get("exit_code")
            try:
                exit_code = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                exit_code = None
    return kind, exit_code


@dataclass
class StatsReport:
    """Structured gauge-board metrics; :meth:`to_dict` is the stable ``--json`` schema."""

    now: str
    since: str | None = None
    window_from: str | None = None  # effective lower bound (since or earliest done)
    window_days: float | None = None  # actual span; may be < 1
    runs_scanned: int = 0
    runs_skipped: int = 0
    runs_in_window: int = 0
    completed: int = 0
    halted: int = 0
    throughput_by_pipeline: dict[str, int] = field(default_factory=dict)
    throughput_by_origin: dict[str, int] = field(default_factory=dict)
    completed_per_day: float | None = None  # only when window_days >= 1
    # Step success (every step-done vs retry / terminal fail) — not validator health.
    step_success_passes: int = 0
    step_success_fails: int = 0
    # Validator-bearing only: non-empty artifacts on step-done vs validator fails.
    validator_passes: int = 0
    validator_fails: int = 0
    retry_events: int = 0
    retry_by_node: dict[str, int] = field(default_factory=dict)
    gate_latencies_s: list[float] = field(default_factory=list)
    human_gates: int = 0
    machine_gates: int = 0
    other_gates: int = 0  # unverified / unknown by
    # Resolved blocked parks only (halt → resume). Snapshot lives separately.
    blocked_time_resolved_s: float = 0.0
    blocked_time_resolved_runs: int = 0
    currently_blocked_count: int = 0
    currently_blocked_oldest_age_s: float | None = None
    currently_blocked_ages_s: list[float] = field(default_factory=list)
    triggers: dict[str, dict[str, Any]] = field(default_factory=dict)
    skipped_ids: list[str] = field(default_factory=list)

    @property
    def step_success_rate(self) -> float | None:
        total = self.step_success_passes + self.step_success_fails
        if total == 0:
            return None
        return self.step_success_passes / total

    @property
    def validator_pass_rate(self) -> float | None:
        total = self.validator_passes + self.validator_fails
        if total == 0:
            return None
        return self.validator_passes / total

    @property
    def retry_per_run(self) -> float | None:
        if self.runs_in_window == 0:
            return None
        return self.retry_events / self.runs_in_window

    @property
    def human_answered_share(self) -> float | None:
        total = self.human_gates + self.machine_gates
        if total == 0:
            return None
        return self.human_gates / total

    @property
    def gate_latency_mean(self) -> float | None:
        if not self.gate_latencies_s:
            return None
        return statistics.fmean(self.gate_latencies_s)

    @property
    def gate_latency_median(self) -> float | None:
        if not self.gate_latencies_s:
            return None
        return float(statistics.median(self.gate_latencies_s))

    @property
    def gate_latency_p90(self) -> float | None:
        return _percentile(sorted(self.gate_latencies_s), 90.0)

    @property
    def blocked_time_resolved_mean_s(self) -> float | None:
        if self.blocked_time_resolved_runs == 0:
            return None
        return self.blocked_time_resolved_s / self.blocked_time_resolved_runs

    def to_dict(self) -> dict[str, Any]:
        """Stable ``--json`` schema (schema_version bumps on breaking changes)."""
        return {
            "schema_version": SCHEMA_VERSION,
            "window": {
                "since": self.since,
                "from": self.window_from,
                "until": self.now,
                "days": self.window_days,
                "per_day_requires_days": MIN_PER_DAY_WINDOW_DAYS,
            },
            "aggregates": {
                "runs": {
                    "scanned": self.runs_scanned,
                    "skipped": self.runs_skipped,
                    "in_window": self.runs_in_window,
                    "completed": self.completed,
                    "halted": self.halted,
                    "skipped_ids": list(self.skipped_ids),
                },
                "throughput": {
                    "completed": self.completed,
                    "per_day": self.completed_per_day,
                    "by_pipeline": dict(sorted(self.throughput_by_pipeline.items())),
                    "by_origin": dict(sorted(self.throughput_by_origin.items())),
                    "note": (
                        "per_day is null when window.days < 1 "
                        "(no sub-day inflation); use completed + window.days"
                    ),
                },
                "step_success": {
                    "passes": self.step_success_passes,
                    "fails": self.step_success_fails,
                    "pass_rate": self.step_success_rate,
                    "signal": (
                        "step-done=pass; retry + step-fail(validator_reasons)=fail "
                        "(all steps, including those with no produces)"
                    ),
                },
                "validator": {
                    "passes": self.validator_passes,
                    "fails": self.validator_fails,
                    "pass_rate": self.validator_pass_rate,
                    "signal": (
                        "step-done with non-empty data.artifacts=pass; "
                        "retry + step-fail(validator_reasons)=fail"
                    ),
                },
                "retry": {
                    "events": self.retry_events,
                    "per_run": self.retry_per_run,
                    "by_node": dict(sorted(self.retry_by_node.items())),
                },
                "gate_latency_s": {
                    "n": len(self.gate_latencies_s),
                    "mean": self.gate_latency_mean,
                    "median": self.gate_latency_median,
                    "p90": self.gate_latency_p90,
                    "signal": (
                        "gate-pending.at → gate-answered.at "
                        "(else gates/<name>.json at, else mtime last-resort)"
                    ),
                },
                "human_answered_share": {
                    "label": HUMAN_ANSWERED_LABEL,
                    "scope": HUMAN_ANSWERED_SCOPE,
                    "human": self.human_gates,
                    "machine": self.machine_gates,
                    "other": self.other_gates,
                    "share": self.human_answered_share,
                    "human_by": sorted(HUMAN_BY),
                    "machine_by": ["default", "flag", "lane:*"],
                    "signal": (
                        "by from gate-answered trail event, else MAC-verified "
                        "gates/<name>.json; forged/unsigned → other"
                    ),
                },
                "blocked_time_resolved_s": {
                    "total": self.blocked_time_resolved_s,
                    "runs": self.blocked_time_resolved_runs,
                    "mean_per_run": self.blocked_time_resolved_mean_s,
                    "signal": "run-halt exit_code=9 → next trail event (closed parks only)",
                },
            },
            "snapshots": {
                "currently_blocked": {
                    "count": self.currently_blocked_count,
                    "oldest_age_s": self.currently_blocked_oldest_age_s,
                    "ages_s": list(self.currently_blocked_ages_s),
                    "label": "point-in-time snapshot (not summed into resolved mean)",
                    "signal": "run-halt exit_code=9 with no subsequent trail event",
                },
                "triggers": self.triggers,
            },
        }


def _read_run_meta(run_dir: Path) -> dict[str, Any] | None:
    """Best-effort run.json (no schema validation — older/partial runs still count)."""
    path = run_dir / RUN_JSON
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _event_in_window(at: str | None, since_dt: datetime | None) -> bool:
    if since_dt is None:
        return True
    dt = parse_at(at)
    if dt is None:
        return False
    return dt >= since_dt


def _run_in_window(events: list[dict], meta: dict, since_dt: datetime | None) -> bool:
    """Include a run when any terminal/start ``at`` or created_at is in the window."""
    if since_dt is None:
        return True
    for ev in events:
        if _event_in_window(ev.get("at") if isinstance(ev.get("at"), str) else None, since_dt):
            return True
    created = meta.get("created_at")
    if isinstance(created, str) and _event_in_window(created, since_dt):
        return True
    return False


def _verify_gate_file_by(run_dir: Path, gate_name: str, doc: dict[str, Any]) -> str | None:
    """Return authenticated ``by`` from a gate decision file, or None if unverified.

    MAC-only check (options list not required for the share gauge). Missing secret
    or bad MAC → None (fail closed — forged files do not enter human/machine).
    """
    mac = doc.get("mac")
    if not isinstance(mac, str) or not mac:
        return None
    secret = load_run_key(run_dir)
    if secret is None:
        return None
    choice, by, at = doc.get("choice"), doc.get("by"), doc.get("at")
    if not isinstance(by, str) or not by:
        return None
    expected = compute_mac(secret, run_dir, gate_name, choice, by, at)
    if not hmac.compare_digest(expected, mac):
        return None
    return by


def _scan_gates(
    run_dir: Path,
    events: list[dict],
    *,
    is_done: bool,
    report: StatsReport,
) -> None:
    """Accumulate gate latency (all answered) and human-share (done runs only)."""
    pending_first: dict[str, datetime] = {}
    answered_at: dict[str, datetime] = {}
    answered_by: dict[str, str] = {}
    for ev in events:
        kind = ev.get("event")
        node = ev.get("node")
        if not isinstance(node, str) or not node:
            continue
        if kind == "gate-pending":
            dt = parse_at(ev.get("at") if isinstance(ev.get("at"), str) else None)
            if dt is not None and node not in pending_first:
                pending_first[node] = dt
        elif kind == "gate-answered":
            dt = parse_at(ev.get("at") if isinstance(ev.get("at"), str) else None)
            if dt is not None:
                answered_at[node] = dt  # last answer wins
            data = ev.get("data") or {}
            by = data.get("by") if isinstance(data, dict) else None
            if isinstance(by, str) and by:
                answered_by[node] = by

    gates_dir = run_dir / GATE_DIR
    gate_names: set[str] = set(pending_first) | set(answered_at) | set(answered_by)
    file_docs: dict[str, tuple[Path, dict[str, Any] | None]] = {}
    if gates_dir.is_dir():
        for path in sorted(gates_dir.iterdir()):
            if not path.is_file():
                continue
            name = path.name
            if not name.endswith(".json") or name.endswith(".tmp"):
                continue
            gate_name = name[: -len(".json")]
            gate_names.add(gate_name)
            try:
                raw = path.read_text(encoding="utf-8")
                doc = json.loads(raw)
                if not isinstance(doc, dict):
                    doc = None
            except (OSError, ValueError, json.JSONDecodeError):
                doc = None
            file_docs[gate_name] = (path, doc)

    for gate_name in sorted(gate_names):
        pend = pending_first.get(gate_name)
        # Decision time priority: gate-answered.at → file at → mtime
        dec: datetime | None = answered_at.get(gate_name)
        path_doc = file_docs.get(gate_name)
        if dec is None and path_doc is not None:
            path, doc = path_doc
            if doc is not None:
                dec = parse_at(doc.get("at") if isinstance(doc.get("at"), str) else None)
            if dec is None:
                try:
                    dec = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                except OSError:
                    dec = None
        if pend is not None and dec is not None:
            if dec >= pend:
                report.gate_latencies_s.append(_secs(pend, dec))
            # else clock skew / negative — drop from percentiles (not clamped in)

        if not is_done:
            continue
        # Human-share: trail gate-answered.by first, else MAC-verified file.
        by: str | None = answered_by.get(gate_name)
        if by is None and path_doc is not None and path_doc[1] is not None:
            by = _verify_gate_file_by(run_dir, gate_name, path_doc[1])
        if by is None:
            # Only count a gate slot when we have some evidence of a decision
            # (answered event or decision file present). Pending-only → skip.
            if gate_name in answered_at or gate_name in file_docs:
                report.other_gates += 1
            continue
        if _is_human_by(by):
            report.human_gates += 1
        elif _is_machine_by(by):
            report.machine_gates += 1
        else:
            report.other_gates += 1


def _blocked_parks(
    events: list[dict], now: datetime
) -> tuple[float, int, list[float]]:
    """Resolved blocked seconds + run count; ages of still-parked BLOCKED(9) parks.

    A park is **resolved** when a later trail event exists (resume/continuation).
    Still-parked parks are returned as ages (now − halt_at) for the snapshot only —
    never summed into the resolved total/mean.
    """
    resolved_total = 0.0
    resolved_runs = 0
    open_ages: list[float] = []
    n = len(events)
    for i, ev in enumerate(events):
        if ev.get("event") != "run-halt":
            continue
        data = ev.get("data") or {}
        try:
            code = int(data.get("exit_code")) if data.get("exit_code") is not None else None
        except (TypeError, ValueError):
            code = None
        if code != int(ExitCode.BLOCKED):
            continue
        halt_at = parse_at(ev.get("at") if isinstance(ev.get("at"), str) else None)
        if halt_at is None:
            continue
        end: datetime | None = None
        if i + 1 < n:
            nxt = parse_at(
                events[i + 1].get("at") if isinstance(events[i + 1].get("at"), str) else None
            )
            if nxt is not None:
                end = nxt
        if end is not None:
            resolved_total += _secs(halt_at, end)
            resolved_runs += 1
        else:
            open_ages.append(_secs(halt_at, now))
    return resolved_total, resolved_runs, open_ages


def _artifacts_nonempty(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    arts = data.get("artifacts")
    return isinstance(arts, list) and len(arts) > 0


def _accumulate_trail_metrics(events: list[dict], report: StatsReport) -> None:
    for ev in events:
        kind = ev.get("event")
        data = ev.get("data") or {}
        if kind == "step-done":
            report.step_success_passes += 1
            if _artifacts_nonempty(data):
                report.validator_passes += 1
        elif kind == "retry":
            report.retry_events += 1
            report.step_success_fails += 1
            report.validator_fails += 1  # retries carry validator_reasons
            node = ev.get("node") if isinstance(ev.get("node"), str) else "?"
            report.retry_by_node[node] = report.retry_by_node.get(node, 0) + 1
        elif kind == "step-fail":
            reasons = data.get("validator_reasons") if isinstance(data, dict) else None
            if reasons:
                report.step_success_fails += 1
                report.validator_fails += 1


def collect_stats(
    runs_root: Path,
    now: datetime,
    *,
    since: str | None = None,
    trigger_watches: dict[str, Path] | None = None,
) -> StatsReport:
    """Compute the gauge board over ``runs_root`` (+ optional trigger watch dirs).

    ``now`` must be timezone-aware (UTC). ``trigger_watches`` maps trigger name →
    absolute watch dir for depth / lease / circuit snapshots. Pure read — never
    mutates the tree.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    since_dt = parse_since(since) if since else None
    report = StatsReport(now=_format_now(now), since=since)

    root = Path(runs_root)
    if not root.is_dir():
        _fill_triggers(report, trigger_watches, now)
        return report

    earliest_done: datetime | None = None
    open_blocked_ages: list[float] = []

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / RUN_JSON).is_file():
            if (entry / TRAIL_NAME).exists():
                report.runs_skipped += 1
                report.skipped_ids.append(entry.name)
            continue

        meta = _read_run_meta(entry)
        if meta is None:
            report.runs_skipped += 1
            report.skipped_ids.append(entry.name)
            continue

        report.runs_scanned += 1
        try:
            events = list(read_trail(entry))
        except OSError:
            report.runs_skipped += 1
            report.skipped_ids.append(entry.name)
            report.runs_scanned -= 1
            continue

        if not _run_in_window(events, meta, since_dt):
            continue
        report.runs_in_window += 1

        pipeline = meta.get("pipeline") if isinstance(meta.get("pipeline"), str) else "?"
        origin = meta.get("origin") if isinstance(meta.get("origin"), str) else None
        origin_key = origin if origin else "(none)"

        term_kind, _exit = terminal_from_events(events)
        is_done = term_kind == "done"
        is_halt = term_kind == "halt"

        if is_done:
            report.completed += 1
            report.throughput_by_pipeline[pipeline] = (
                report.throughput_by_pipeline.get(pipeline, 0) + 1
            )
            report.throughput_by_origin[origin_key] = (
                report.throughput_by_origin.get(origin_key, 0) + 1
            )
            for ev in events:
                if ev.get("event") != "run-done":
                    continue
                done_at = parse_at(ev.get("at") if isinstance(ev.get("at"), str) else None)
                if done_at is None:
                    continue
                if earliest_done is None or done_at < earliest_done:
                    earliest_done = done_at
        elif is_halt:
            report.halted += 1

        _accumulate_trail_metrics(events, report)
        _scan_gates(entry, events, is_done=is_done, report=report)
        resolved_s, resolved_n, open_ages = _blocked_parks(events, now)
        if resolved_n:
            report.blocked_time_resolved_s += resolved_s
            report.blocked_time_resolved_runs += resolved_n
        open_blocked_ages.extend(open_ages)

    # Snapshot: currently blocked (still parked)
    if open_blocked_ages:
        report.currently_blocked_count = len(open_blocked_ages)
        report.currently_blocked_ages_s = sorted(open_blocked_ages, reverse=True)
        report.currently_blocked_oldest_age_s = max(open_blocked_ages)

    # Window length + per_day honesty: never inflate sub-day windows to ×24.
    if since_dt is not None:
        report.window_from = (
            since_dt.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        report.window_days = max(_secs(since_dt, now) / 86400.0, 0.0)
    elif earliest_done is not None:
        report.window_from = (
            earliest_done.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        report.window_days = max(_secs(earliest_done, now) / 86400.0, 0.0)
    else:
        report.window_from = None
        report.window_days = None

    if (
        report.window_days is not None
        and report.window_days >= MIN_PER_DAY_WINDOW_DAYS
        and report.completed > 0
    ):
        report.completed_per_day = float(report.completed) / report.window_days
    else:
        # Sub-day window or no completions → no per_day rate (avoids ×24 inflation).
        report.completed_per_day = None

    _fill_triggers(report, trigger_watches, now)
    return report


def _fill_triggers(
    report: StatsReport,
    trigger_watches: dict[str, Path] | None,
    now: datetime,
) -> None:
    if not trigger_watches:
        return
    now_ts = now.timestamp()
    for name, watch in sorted(trigger_watches.items()):
        watch_p = Path(watch)
        depths: dict[str, int] = {
            k: 0
            for k in (
                "needs_human",
                "blocked",
                "capacity",
                "waiting",
                "claimed",
                "inflight",
                "spool",
                "failed",
                "done",
                "stuck",
            )
        }
        if watch_p.is_dir():
            try:
                depths = count_by_class(watch_p)
            except OSError:
                pass

        circuit = read_circuit(watch_p) if watch_p.is_dir() else {"consecutive_failures": 0}
        consecutive = int(circuit.get("consecutive_failures", 0) or 0)
        opened_at = circuit.get("opened_at")
        if not isinstance(opened_at, str):
            opened_at = None
        opened = opened_at is not None or consecutive > 0

        leases_raw: dict[str, Any] = {
            "lease_count": 0,
            "expired_live": 0,
            "missing_lease": 0,
        }
        if watch_p.is_dir():
            try:
                leases_raw = lease_status(watch_p, now=now_ts)
            except OSError:
                pass

        report.triggers[name] = {
            "depths": {
                k: int(depths.get(k, 0) or 0)
                for k in (
                    "needs_human",
                    "blocked",
                    "capacity",
                    "waiting",
                    "claimed",
                    "inflight",
                    "spool",
                    "failed",
                    "done",
                )
            },
            "circuit": {
                "consecutive_failures": consecutive,
                "opened": bool(opened and consecutive > 0),
                "opened_at": opened_at,
            },
            "leases": {
                "count": int(leases_raw.get("lease_count", 0) or 0),
                "expired_live": int(leases_raw.get("expired_live", 0) or 0),
                "missing": int(leases_raw.get("missing_lease", 0) or 0),
                "historical_reaped": None,
            },
        }


def _fmt_rate(rate: float | None) -> str:
    if rate is None:
        return "n/a"
    return f"{rate * 100:.1f}%"


def _fmt_num(n: float | None, unit: str = "") -> str:
    if n is None:
        return "n/a"
    if abs(n) >= 100:
        body = f"{n:.0f}"
    elif abs(n) >= 10:
        body = f"{n:.1f}"
    else:
        body = f"{n:.2f}"
    return f"{body}{unit}"


def render_stats(report: StatsReport) -> str:
    """Human-readable grouped table (default CLI view)."""
    if report.runs_scanned == 0 and report.runs_skipped == 0 and not report.triggers:
        return "cairn: no runs found"

    lines: list[str] = []
    # Window is always explicit (from / until / days) — no silent all-time rate.
    if report.window_from:
        win = f"from {report.window_from} → {report.now}"
    elif report.since:
        win = f"since {report.since} → {report.now}"
    else:
        win = f"all time → {report.now}"
    days_s = _fmt_num(report.window_days, "d") if report.window_days is not None else "n/a"
    lines.append(f"cairn stats  ({win}; window_days={days_s})")
    lines.append("")
    lines.append("=== WINDOW AGGREGATES ===")
    lines.append("RUNS")
    lines.append(f"  scanned={report.runs_scanned}")
    lines.append(f"  in_window={report.runs_in_window}")
    lines.append(f"  completed={report.completed}")
    lines.append(f"  halted={report.halted}")
    lines.append(f"  skipped={report.runs_skipped}")
    lines.append("")
    lines.append("THROUGHPUT")
    lines.append(f"  completed={report.completed}")
    if report.completed_per_day is not None:
        lines.append(
            f"  per_day={_fmt_num(report.completed_per_day)}  "
            f"(window {_fmt_num(report.window_days)}d)"
        )
    else:
        reason = (
            "window < 1d — no rate (avoids sub-day inflation)"
            if report.window_days is not None and report.window_days < MIN_PER_DAY_WINDOW_DAYS
            else "n/a"
        )
        lines.append(f"  per_day=n/a  ({reason})")
    if report.throughput_by_pipeline:
        lines.append("  by pipeline:")
        for k, v in sorted(report.throughput_by_pipeline.items()):
            lines.append(f"    {k:24} {v}")
    if report.throughput_by_origin:
        lines.append("  by origin:")
        for k, v in sorted(report.throughput_by_origin.items()):
            lines.append(f"    {k:24} {v}")
    lines.append("")
    lines.append("STEP SUCCESS")
    lines.append(f"  passes={report.step_success_passes}")
    lines.append(f"  fails={report.step_success_fails}")
    lines.append(f"  pass_rate={_fmt_rate(report.step_success_rate)}")
    lines.append("  (all steps; not validator health)")
    lines.append("")
    lines.append("VALIDATOR")
    lines.append(f"  passes={report.validator_passes}")
    lines.append(f"  fails={report.validator_fails}")
    lines.append(f"  pass_rate={_fmt_rate(report.validator_pass_rate)}")
    lines.append("  (step-done with non-empty artifacts only)")
    lines.append("")
    lines.append("RETRY")
    lines.append(f"  events={report.retry_events}")
    lines.append(f"  per_run={_fmt_num(report.retry_per_run)}")
    if report.retry_by_node:
        lines.append("  by node:")
        for k, v in sorted(report.retry_by_node.items()):
            lines.append(f"    {k:24} {v}")
    lines.append("")
    lines.append("GATE LATENCY (s)")
    lines.append(f"  n={len(report.gate_latencies_s)}")
    lines.append(f"  mean={_fmt_num(report.gate_latency_mean)}")
    lines.append(f"  median={_fmt_num(report.gate_latency_median)}")
    lines.append(f"  p90={_fmt_num(report.gate_latency_p90)}")
    lines.append("")
    lines.append(HUMAN_ANSWERED_LABEL.upper())
    lines.append(f"  human={report.human_gates}")
    lines.append(f"  machine={report.machine_gates}")
    lines.append(f"  other={report.other_gates}")
    lines.append(f"  share={_fmt_rate(report.human_answered_share)}")
    lines.append(f"  ({HUMAN_ANSWERED_SCOPE}; by: tty|external = human; default|flag|lane:* = machine)")
    lines.append("")
    lines.append("BLOCKED TIME RESOLVED (s)")
    lines.append(f"  total={_fmt_num(report.blocked_time_resolved_s)}")
    lines.append(f"  runs={report.blocked_time_resolved_runs}")
    lines.append(f"  mean/run={_fmt_num(report.blocked_time_resolved_mean_s)}")
    lines.append("  (closed parks only: halt→resume)")
    lines.append("")
    lines.append("=== SNAPSHOTS (point-in-time) ===")
    lines.append("CURRENTLY BLOCKED")
    lines.append(f"  count={report.currently_blocked_count}")
    lines.append(f"  oldest_age_s={_fmt_num(report.currently_blocked_oldest_age_s)}")
    lines.append("  (snapshot — not in resolved mean)")
    lines.append("")
    if report.triggers:
        lines.append("TRIGGERS")
        for name, t in report.triggers.items():
            d = t.get("depths") or {}
            c = t.get("circuit") or {}
            L = t.get("leases") or {}
            lines.append(
                f"  {name}: waiting={d.get('waiting', 0)}"
                f" needs_human={d.get('needs_human', 0)}"
                f" blocked={d.get('blocked', 0)}"
                f" capacity={d.get('capacity', 0)}"
                f" claimed={d.get('claimed', 0)}"
                f" spool={d.get('spool', 0)}"
            )
            open_s = "open" if c.get("opened") else "closed"
            lines.append(
                f"    circuit={open_s} consecutive={c.get('consecutive_failures', 0)}"
            )
            lines.append(
                f"  leases={L.get('count', 0)}"
                f" expired_live={L.get('expired_live', 0)}"
                f" missing={L.get('missing', 0)}"
            )
    else:
        lines.append("TRIGGERS")
        lines.append("  (none declared)")
    return "\n".join(lines)
