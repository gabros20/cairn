"""W7 — cairn stats: read-only factory gauge board over trails + gates + ledgers."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cairn.cli import main
from cairn.kernel import newkit
from cairn.kernel.gatekeys import compute_mac, ensure_run_key
from cairn.kernel.queue_ledger import pointer_path, write_circuit, write_pointer
from cairn.kernel.statskit import (
    HUMAN_ANSWERED_LABEL,
    HUMAN_ANSWERED_SCOPE,
    collect_stats,
    render_stats,
)
from cairn.kernel.types import ExitCode

NOW = datetime(2026, 7, 23, 18, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Fabric helpers — write trail/gates with fixed timestamps (no wall clock).
# --------------------------------------------------------------------------- #


def _meta(
    run_id: str,
    *,
    pipeline: str = "ship",
    origin: str | None = None,
    created_at: str = "2026-07-20T10:00:00.000Z",
) -> dict:
    doc = {
        "run_id": run_id,
        "pipeline": pipeline,
        "pipeline_hash": "sha256:abc",
        "cairn_version": "0.1.0",
        "params": {},
        "dims": {},
        "executors": {"default": "stub"},
        "models": {},
        "created_at": created_at,
        "status": "done",
        "nodes": {},
    }
    if origin is not None:
        doc["origin"] = origin
    return doc


def _write_trail(run_dir: Path, run_id: str, events: list[tuple]) -> None:
    """events: (at, event, node|None, data|None) — fixed `at` for determinism."""
    lines = []
    for i, row in enumerate(events, start=1):
        at, event, node, data = row
        env = {
            "v": 1,
            "seq": i,
            "at": at,
            "run_id": run_id,
            "event": event,
            "node": node,
            "attempt": None,
            "cycle": None,
            "data": data if data is not None else {},
        }
        lines.append(json.dumps(env, ensure_ascii=False))
    (run_dir / "trail.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_gate(
    run_dir: Path,
    name: str,
    *,
    by: str,
    at: str,
    choice: str = "yes",
    sign: bool = False,
) -> None:
    gdir = run_dir / "gates"
    gdir.mkdir(parents=True, exist_ok=True)
    payload: dict = {"choice": choice, "by": by, "at": at}
    if sign:
        secret = ensure_run_key(run_dir)
        payload["mac"] = compute_mac(secret, run_dir, name, choice, by, at)
    (gdir / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _mk_run(
    root: Path,
    run_id: str,
    events: list[tuple],
    *,
    pipeline: str = "ship",
    origin: str | None = None,
    gates: list[tuple] | None = None,
    sign_gates: bool = False,
    meta_extra: dict | None = None,
) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    doc = _meta(run_id, pipeline=pipeline, origin=origin)
    if meta_extra:
        doc.update(meta_extra)
    (run_dir / "run.json").write_text(json.dumps(doc), encoding="utf-8")
    _write_trail(run_dir, run_id, events)
    for g in gates or []:
        # (name, by, at) or (name, by, at, choice)
        name, by, at = g[0], g[1], g[2]
        _write_gate(run_dir, name, by=by, at=at, sign=sign_gates)
    return run_dir


def _done_events(
    *,
    start: str = "2026-07-21T10:00:00.000Z",
    done: str = "2026-07-21T11:00:00.000Z",
    steps: list[tuple] | None = None,
    extras: list[tuple] | None = None,
) -> list[tuple]:
    """Build a minimal completed-run trail (+ optional step/retry/gate events)."""
    evs: list[tuple] = [(start, "run-start", None, None)]
    if steps:
        evs.extend(steps)
    if extras:
        evs.extend(extras)
    evs.append((done, "run-done", None, None))
    return evs


# --------------------------------------------------------------------------- #
# Metric unit tests
# --------------------------------------------------------------------------- #


def test_empty_runs_root_is_clean(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir()
    report = collect_stats(root, now=NOW)
    assert report.runs_scanned == 0
    assert report.completed == 0
    assert report.step_success_rate is None
    assert report.validator_pass_rate is None
    assert report.human_answered_share is None
    assert "no runs" in render_stats(report)


def test_missing_runs_root_is_clean(tmp_path: Path):
    report = collect_stats(tmp_path / "nope", now=NOW)
    assert report.runs_scanned == 0
    d = report.to_dict()
    assert d["schema_version"] == 2
    assert d["aggregates"]["runs"]["scanned"] == 0


def test_throughput_by_pipeline_and_origin(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir()
    _mk_run(
        root,
        "a",
        _done_events(done="2026-07-21T12:00:00.000Z"),
        pipeline="ship",
        origin="trigger:inbox",
    )
    _mk_run(
        root,
        "b",
        _done_events(done="2026-07-22T12:00:00.000Z"),
        pipeline="ship",
        origin="trigger:inbox",
    )
    _mk_run(
        root,
        "c",
        _done_events(done="2026-07-22T14:00:00.000Z"),
        pipeline="docs",
        origin=None,
    )
    _mk_run(
        root,
        "d",
        [
            ("2026-07-22T15:00:00.000Z", "run-start", None, None),
            ("2026-07-22T15:01:00.000Z", "run-halt", "g", {"exit_code": 6}),
        ],
        pipeline="ship",
    )

    report = collect_stats(root, now=NOW)
    assert report.completed == 3
    assert report.halted == 1
    assert report.throughput_by_pipeline == {"ship": 2, "docs": 1}
    assert report.throughput_by_origin == {"trigger:inbox": 2, "(none)": 1}
    # Window from earliest done (2026-07-21) to NOW is > 1 day → per_day set.
    assert report.window_days is not None and report.window_days >= 1.0
    assert report.completed_per_day is not None
    assert report.completed_per_day == pytest.approx(3 / report.window_days)


def test_per_day_not_inflated_on_sub_day_window(tmp_path: Path):
    """I2: a run done 1h ago must not report per_day≈24 under a sub-day window."""
    root = tmp_path / "runs"
    root.mkdir()
    _mk_run(
        root,
        "fresh",
        _done_events(
            start="2026-07-23T16:00:00.000Z",
            done="2026-07-23T17:00:00.000Z",
        ),
        meta_extra={"created_at": "2026-07-23T16:00:00.000Z"},
    )
    report = collect_stats(root, now=NOW)
    assert report.completed == 1
    assert report.window_days is not None
    assert report.window_days < 1.0
    assert report.completed_per_day is None  # no ×24 inflation
    text = render_stats(report)
    assert "window_days=" in text
    assert "per_day=n/a" in text
    assert "sub-day" in text or "< 1d" in text
    d = report.to_dict()
    assert d["window"]["days"] == pytest.approx(report.window_days)
    assert d["window"]["from"] is not None
    assert d["aggregates"]["throughput"]["per_day"] is None
    assert d["aggregates"]["throughput"]["completed"] == 1


def test_step_success_vs_validator_pass_distinction(tmp_path: Path):
    """I1: bare step-done counts toward step_success but NOT validator_pass."""
    root = tmp_path / "runs"
    root.mkdir()
    steps = [
        # No produces → empty artifacts → step success only
        ("2026-07-21T10:01:00.000Z", "step-done", "greet", {"artifacts": []}),
        # Real produces check → validator pass
        (
            "2026-07-21T10:02:00.000Z",
            "step-done",
            "build",
            {"artifacts": ["artifacts/out.json"]},
        ),
        (
            "2026-07-21T10:03:00.000Z",
            "retry",
            "qa",
            {"validator_reasons": ["missing report"]},
        ),
        (
            "2026-07-21T10:04:00.000Z",
            "step-fail",
            "qa",
            {"validator_reasons": ["missing report"]},
        ),
    ]
    _mk_run(
        root,
        "v1",
        [
            ("2026-07-21T10:00:00.000Z", "run-start", None, None),
            *steps,
            ("2026-07-21T10:05:00.000Z", "run-halt", "qa", {"exit_code": 3}),
        ],
    )
    report = collect_stats(root, now=NOW)
    # step success: 2 done + 2 fails (retry + terminal fail)
    assert report.step_success_passes == 2
    assert report.step_success_fails == 2
    assert report.step_success_rate == pytest.approx(0.5)
    # validator: only the build step-done with artifacts; fails = retry + step-fail
    assert report.validator_passes == 1
    assert report.validator_fails == 2
    assert report.validator_pass_rate == pytest.approx(1 / 3)
    d = report.to_dict()
    assert "step_success" in d["aggregates"]
    assert d["aggregates"]["step_success"]["pass_rate"] == pytest.approx(0.5)
    assert d["aggregates"]["validator"]["pass_rate"] == pytest.approx(1 / 3)


def test_no_validator_run_step_success_only(tmp_path: Path):
    """A pipeline with no produces: step_success=100%, validator rate n/a."""
    root = tmp_path / "runs"
    root.mkdir()
    _mk_run(
        root,
        "bare",
        _done_events(
            steps=[
                ("2026-07-21T10:01:00.000Z", "step-done", "a", {"artifacts": []}),
                ("2026-07-21T10:02:00.000Z", "step-done", "b", {}),
            ],
        ),
    )
    report = collect_stats(root, now=NOW)
    assert report.step_success_passes == 2
    assert report.step_success_fails == 0
    assert report.step_success_rate == pytest.approx(1.0)
    assert report.validator_passes == 0
    assert report.validator_fails == 0
    assert report.validator_pass_rate is None


def test_gate_latency_prefers_gate_answered_trail_event(tmp_path: Path):
    """M1: gate-answered.at beats file at / mtime."""
    root = tmp_path / "runs"
    root.mkdir()
    _mk_run(
        root,
        "g1",
        _done_events(
            extras=[
                (
                    "2026-07-21T10:10:00.000Z",
                    "gate-pending",
                    "shipit",
                    {"question": "?", "options": ["yes", "no"]},
                ),
                (
                    "2026-07-21T10:12:00.000Z",
                    "gate-answered",
                    "shipit",
                    {"choice": "yes", "by": "tty"},
                ),
            ],
            done="2026-07-21T10:30:00.000Z",
        ),
        # Deliberately different file at — trail event must win (120s not 600s)
        gates=[("shipit", "tty", "2026-07-21T10:20:00.000Z")],
    )
    report = collect_stats(root, now=NOW)
    assert len(report.gate_latencies_s) == 1
    assert report.gate_latencies_s[0] == pytest.approx(120.0)


def test_human_answered_share_from_gate_answered_and_verified_file(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir()
    # Mix of by values via trail gate-answered (primary honest source)
    extras = [
        ("2026-07-21T10:10:00.000Z", "gate-pending", "a", {}),
        ("2026-07-21T10:11:00.000Z", "gate-pending", "b", {}),
        ("2026-07-21T10:12:00.000Z", "gate-pending", "c", {}),
        ("2026-07-21T10:13:00.000Z", "gate-pending", "d", {}),
        ("2026-07-21T10:14:00.000Z", "gate-pending", "e", {}),
        ("2026-07-21T10:15:00.000Z", "gate-answered", "a", {"choice": "y", "by": "tty"}),
        ("2026-07-21T10:16:00.000Z", "gate-answered", "b", {"choice": "y", "by": "external"}),
        ("2026-07-21T10:17:00.000Z", "gate-answered", "c", {"choice": "y", "by": "default"}),
        ("2026-07-21T10:18:00.000Z", "gate-answered", "d", {"choice": "y", "by": "flag"}),
        ("2026-07-21T10:19:00.000Z", "gate-answered", "e", {"choice": "y", "by": "lane:lit"}),
    ]
    _mk_run(root, "done-mixed", _done_events(extras=extras))

    # Halted run's gates must NOT enter the human-share denominator
    _mk_run(
        root,
        "halted-gates",
        [
            ("2026-07-21T11:00:00.000Z", "run-start", None, None),
            ("2026-07-21T11:01:00.000Z", "gate-pending", "x", {}),
            ("2026-07-21T11:02:00.000Z", "gate-answered", "x", {"choice": "y", "by": "tty"}),
            ("2026-07-21T11:03:00.000Z", "run-halt", "x", {"exit_code": 6}),
        ],
    )

    report = collect_stats(root, now=NOW)
    assert report.human_gates == 2
    assert report.machine_gates == 3
    assert report.human_answered_share == pytest.approx(2 / 5)
    d = report.to_dict()
    has = d["aggregates"]["human_answered_share"]
    assert has["label"] == HUMAN_ANSWERED_LABEL
    assert has["scope"] == HUMAN_ANSWERED_SCOPE
    assert "completed runs" in has["scope"]
    text = render_stats(report)
    assert "completed runs" in text


def test_unsigned_gate_file_does_not_skew_human_share(tmp_path: Path):
    """M2: raw unverified by on disk must not count as human/machine."""
    root = tmp_path / "runs"
    root.mkdir()
    _mk_run(
        root,
        "forged",
        _done_events(
            extras=[("2026-07-21T10:10:00.000Z", "gate-pending", "g", {})],
        ),
        # unsigned gate file claiming tty
        gates=[("g", "tty", "2026-07-21T10:12:00.000Z")],
        sign_gates=False,
    )
    report = collect_stats(root, now=NOW)
    assert report.human_gates == 0
    assert report.machine_gates == 0
    assert report.other_gates == 1  # present but unverified


def test_mac_verified_gate_file_counts_when_no_trail_answered(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir()
    _mk_run(
        root,
        "signed",
        _done_events(
            extras=[("2026-07-21T10:10:00.000Z", "gate-pending", "g", {})],
        ),
        gates=[("g", "external", "2026-07-21T10:12:00.000Z")],
        sign_gates=True,
    )
    report = collect_stats(root, now=NOW)
    assert report.human_gates == 1
    assert report.machine_gates == 0


def test_blocked_time_resolved_vs_currently_blocked_snapshot(tmp_path: Path):
    """I3: still-parked parks are snapshot only — not in resolved mean."""
    root = tmp_path / "runs"
    root.mkdir()
    # Parked 60s then resumed
    _mk_run(
        root,
        "blocked-resume",
        [
            ("2026-07-21T10:00:00.000Z", "run-start", None, None),
            ("2026-07-21T10:01:00.000Z", "run-halt", "fetch", {"exit_code": 9}),
            ("2026-07-21T10:02:00.000Z", "step-start", "fetch", None),
            ("2026-07-21T10:03:00.000Z", "run-done", None, None),
        ],
    )
    # Still parked — snapshot only
    _mk_run(
        root,
        "blocked-parked",
        [
            ("2026-07-23T17:00:00.000Z", "run-start", None, None),
            ("2026-07-23T17:30:00.000Z", "run-halt", "auth", {"exit_code": 9}),
        ],
    )
    # NEEDS_HUMAN must not count
    _mk_run(
        root,
        "needs-human",
        [
            ("2026-07-21T12:00:00.000Z", "run-start", None, None),
            ("2026-07-21T12:01:00.000Z", "run-halt", "g", {"exit_code": 6}),
        ],
    )

    report = collect_stats(root, now=NOW)
    assert report.blocked_time_resolved_runs == 1
    assert report.blocked_time_resolved_s == pytest.approx(60.0)
    assert report.blocked_time_resolved_mean_s == pytest.approx(60.0)
    assert report.currently_blocked_count == 1
    assert report.currently_blocked_oldest_age_s == pytest.approx(1800.0)
    d = report.to_dict()
    assert d["aggregates"]["blocked_time_resolved_s"]["total"] == pytest.approx(60.0)
    assert d["snapshots"]["currently_blocked"]["count"] == 1
    assert "snapshot" in d["snapshots"]["currently_blocked"]["label"]
    text = render_stats(report)
    assert "SNAPSHOTS" in text
    assert "WINDOW AGGREGATES" in text
    assert "CURRENTLY BLOCKED" in text


def test_corrupt_run_skipped_not_crashed(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir()
    _mk_run(root, "good", _done_events())
    bad = root / "bad"
    bad.mkdir()
    (bad / "run.json").write_text("{not json", encoding="utf-8")
    (bad / "trail.jsonl").write_text(
        json.dumps(
            {
                "v": 1,
                "seq": 1,
                "at": "2026-07-21T10:00:00.000Z",
                "run_id": "bad",
                "event": "run-done",
                "node": None,
                "data": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    orphan = root / "orphan-trail"
    orphan.mkdir()
    (orphan / "trail.jsonl").write_text("{}\n", encoding="utf-8")

    report = collect_stats(root, now=NOW)
    assert report.completed == 1
    assert report.runs_skipped >= 1
    assert "bad" in report.skipped_ids
    assert report.to_dict()["aggregates"]["runs"]["completed"] == 1


def test_since_window_filters_old_runs(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir()
    _mk_run(
        root,
        "old",
        _done_events(
            start="2026-07-01T10:00:00.000Z",
            done="2026-07-01T11:00:00.000Z",
        ),
        meta_extra={"created_at": "2026-07-01T10:00:00.000Z"},
    )
    _mk_run(
        root,
        "new",
        _done_events(
            start="2026-07-22T10:00:00.000Z",
            done="2026-07-22T11:00:00.000Z",
        ),
        meta_extra={"created_at": "2026-07-22T10:00:00.000Z"},
    )
    report = collect_stats(root, now=NOW, since="2026-07-20")
    assert report.completed == 1
    assert report.runs_in_window == 1
    assert report.since == "2026-07-20"
    assert report.window_from is not None


def test_trigger_depths_circuit_and_leases(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir()
    watch = tmp_path / "inbox"
    watch.mkdir()
    (watch / ".waiting").mkdir()
    (watch / ".waiting" / ".runs").mkdir(parents=True)
    (watch / ".waiting" / "item.json").write_text("{}", encoding="utf-8")
    write_pointer(
        pointer_path(watch / ".waiting", "item.json"),
        run_dir=tmp_path / "some-run",
        outcome="waiting",
        exit_code=9,
    )
    write_circuit(watch, consecutive_failures=3, opened_at="2026-07-23T12:00:00.000Z")

    report = collect_stats(root, now=NOW, trigger_watches={"inbox": watch})
    assert "inbox" in report.triggers
    t = report.triggers["inbox"]
    assert t["depths"]["blocked"] >= 1
    assert t["circuit"]["consecutive_failures"] == 3
    assert t["circuit"]["opened"] is True
    assert report.to_dict()["snapshots"]["triggers"]["inbox"]["depths"]["blocked"] >= 1


def test_json_schema_shape_stable(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir()
    _mk_run(root, "r1", _done_events())
    d = collect_stats(root, now=NOW).to_dict()
    assert set(d.keys()) == {"schema_version", "window", "aggregates", "snapshots"}
    assert d["schema_version"] == 2
    assert "from" in d["window"] and "until" in d["window"] and "days" in d["window"]
    ag = d["aggregates"]
    assert "step_success" in ag
    assert "validator" in ag
    assert "blocked_time_resolved_s" in ag
    assert "throughput" in ag
    assert "human_answered_share" in ag
    assert ag["human_answered_share"]["scope"]
    sn = d["snapshots"]
    assert "currently_blocked" in sn
    assert "triggers" in sn


def test_stats_is_strictly_read_only(tmp_path: Path):
    """Snapshot the runs tree before/after — stats must create/modify/delete nothing."""
    root = tmp_path / "runs"
    root.mkdir()
    _mk_run(
        root,
        "ro",
        _done_events(
            extras=[
                ("2026-07-21T10:10:00.000Z", "gate-pending", "g", {}),
                ("2026-07-21T10:12:00.000Z", "gate-answered", "g", {"choice": "y", "by": "tty"}),
            ],
        ),
        gates=[("g", "tty", "2026-07-21T10:12:00.000Z")],
        sign_gates=True,
    )
    watch = tmp_path / "inbox"
    watch.mkdir()
    write_circuit(watch, consecutive_failures=1)

    def snapshot(base: Path) -> dict[str, tuple[int, bytes]]:
        out: dict[str, tuple[int, bytes]] = {}
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames.sort()
            filenames.sort()
            for fn in filenames:
                p = Path(dirpath) / fn
                rel = str(p.relative_to(base))
                st = p.stat()
                out[rel] = (st.st_mtime_ns, p.read_bytes())
            for dn in dirnames:
                p = Path(dirpath) / dn
                rel = str(p.relative_to(base)) + "/"
                out.setdefault(rel, (p.stat().st_mtime_ns, b""))
        return out

    before_runs = snapshot(root)
    before_watch = snapshot(watch)
    collect_stats(root, now=NOW, trigger_watches={"inbox": watch})
    assert snapshot(root) == before_runs
    assert snapshot(watch) == before_watch


def test_no_divide_by_zero_on_empty_metrics(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir()
    _mk_run(
        root,
        "empty-ish",
        [
            ("2026-07-21T10:00:00.000Z", "run-start", None, None),
            ("2026-07-21T10:01:00.000Z", "run-halt", "n", {"exit_code": 4}),
        ],
    )
    report = collect_stats(root, now=NOW)
    assert report.step_success_rate is None
    assert report.validator_pass_rate is None
    assert report.human_answered_share is None
    assert report.gate_latency_mean is None
    assert report.retry_per_run == pytest.approx(0.0)
    text = render_stats(report)
    assert "STEP SUCCESS" in text and "VALIDATOR" in text


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_cli_stats_json_and_table(tmp_path: Path, monkeypatch, capsys):
    ws = newkit.new_workspace("demo", tmp_path)
    runs = ws / "runs"
    runs.mkdir(exist_ok=True)
    _mk_run(
        runs,
        "cli-run",
        _done_events(
            extras=[
                ("2026-07-21T10:10:00.000Z", "step-done", "greet", {"artifacts": []}),
                ("2026-07-21T10:11:00.000Z", "gate-pending", "ok", {}),
                ("2026-07-21T10:11:05.000Z", "gate-answered", "ok", {"choice": "y", "by": "default"}),
            ],
        ),
        pipeline="hello",
        gates=[("ok", "default", "2026-07-21T10:11:05.000Z")],
    )
    monkeypatch.chdir(ws)

    assert main(["stats", "--json"]) == int(ExitCode.OK)
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["schema_version"] == 2
    assert doc["aggregates"]["runs"]["completed"] == 1
    assert doc["aggregates"]["step_success"]["passes"] == 1
    assert doc["aggregates"]["validator"]["passes"] == 0  # empty artifacts
    assert doc["aggregates"]["human_answered_share"]["machine"] == 1
    assert "presence, not comprehension" in doc["aggregates"]["human_answered_share"]["label"]
    assert "aggregates" in doc and "snapshots" in doc

    assert main(["stats"]) == int(ExitCode.OK)
    table = capsys.readouterr().out
    assert "THROUGHPUT" in table
    assert "STEP SUCCESS" in table
    assert "WINDOW AGGREGATES" in table
    assert "SNAPSHOTS" in table


def test_cli_stats_empty_workspace(tmp_path: Path, monkeypatch, capsys):
    ws = newkit.new_workspace("demo", tmp_path)
    monkeypatch.chdir(ws)
    assert main(["stats"]) == int(ExitCode.OK)
    assert "no runs" in capsys.readouterr().out


def test_cli_stats_invalid_since(tmp_path: Path, monkeypatch, capsys):
    ws = newkit.new_workspace("demo", tmp_path)
    monkeypatch.chdir(ws)
    assert main(["stats", "--since", "not-a-date"]) == int(ExitCode.CONFIG)
    err = capsys.readouterr().err
    assert "since" in err.lower() or "invalid" in err.lower()


def test_cli_stats_read_only_over_workspace(tmp_path: Path, monkeypatch, capsys):
    ws = newkit.new_workspace("demo", tmp_path)
    runs = ws / "runs"
    runs.mkdir(exist_ok=True)
    _mk_run(runs, "ro-cli", _done_events())

    def tree_sig(base: Path) -> set[tuple[str, int, bytes]]:
        sig: set[tuple[str, int, bytes]] = set()
        for dirpath, _, filenames in os.walk(base):
            for fn in filenames:
                p = Path(dirpath) / fn
                sig.add((str(p.relative_to(base)), p.stat().st_size, p.read_bytes()))
        return sig

    before = tree_sig(ws)
    monkeypatch.chdir(ws)
    assert main(["stats", "--json"]) == int(ExitCode.OK)
    capsys.readouterr()
    assert tree_sig(ws) == before
