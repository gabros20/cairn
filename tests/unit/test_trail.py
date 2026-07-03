"""Trail Protocol v1: append-only event stream with a monotonic seq consumer offset.

Behaviour-level tests against the public surface (TrailWriter.emit, read_trail, follow,
derive_status) — OBSERVABILITY.md §1 (envelope + guarantees) and §3 (ps status derivation).
"""

from __future__ import annotations

import json

from datetime import datetime, timezone

from cairn.kernel.trail import (
    TrailWriter,
    derive_status,
    follow,
    format_at,
    read_trail,
)

RUN_ID = "acme-redesign-20260703"


def test_format_at_is_z_terminated_utc_for_aware_and_naive():
    # An aware UTC clock round-trips to the canonical Z-terminated, millisecond shape.
    aware = datetime(2026, 7, 3, 10, 14, 2, 113000, tzinfo=timezone.utc)
    assert format_at(aware) == "2026-07-03T10:14:02.113Z"
    # A naive datetime is read AS UTC (the codebase-wide tolerance) — never crashes, no offset.
    naive = datetime(2026, 7, 3, 10, 14, 2, 113000)
    s = format_at(naive)
    assert s == "2026-07-03T10:14:02.113Z"
    # Whatever we write parses straight back with a real tzinfo (no naive-pinning needed).
    assert datetime.fromisoformat(s).tzinfo is not None


def test_emit_appends_one_envelope_line_per_event(tmp_path):
    w = TrailWriter(tmp_path, RUN_ID)
    w.emit("run-start", data={"url": "https://acme.test"})

    lines = (tmp_path / "trail.jsonl").read_text().splitlines()
    assert len(lines) == 1
    ev = json.loads(lines[0])
    assert ev["v"] == 1
    assert ev["seq"] == 1
    assert ev["run_id"] == RUN_ID
    assert ev["event"] == "run-start"
    assert ev["data"] == {"url": "https://acme.test"}
    # ISO-8601, millisecond precision, Z-terminated
    assert ev["at"].endswith("Z") and "." in ev["at"]


def test_seq_is_strictly_monotonic_across_writer_reopen(tmp_path):
    w1 = TrailWriter(tmp_path, RUN_ID)
    w1.emit("run-start")
    w1.emit("step-start", node="capture")
    w1.close()

    # A fresh writer (crash-then-resume) must continue the offset, not restart it.
    w2 = TrailWriter(tmp_path, RUN_ID)
    w2.emit("step-done", node="capture")
    w2.close()

    seqs = [ev["seq"] for ev in read_trail(tmp_path)]
    assert seqs == [1, 2, 3]


def test_reader_tolerates_a_torn_final_line(tmp_path):
    w = TrailWriter(tmp_path, RUN_ID)
    w.emit("run-start")
    w.emit("step-start", node="capture")
    w.close()
    # Simulate a crash mid-append: garbage bytes with no newline as the final line.
    with (tmp_path / "trail.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"v":1,"seq":3,"event":"step-do')

    events = list(read_trail(tmp_path))
    assert [ev["seq"] for ev in events] == [1, 2]
    # A resuming writer also survives the torn tail and keeps counting from the last good seq.
    w2 = TrailWriter(tmp_path, RUN_ID)
    assert w2.emit("step-done")["seq"] == 3


def test_since_filters_already_seen_events(tmp_path):
    w = TrailWriter(tmp_path, RUN_ID)
    for _ in range(5):
        w.emit("heartbeat", node="capture")
    w.close()

    resumed = list(read_trail(tmp_path, since=3))
    assert [ev["seq"] for ev in resumed] == [4, 5]


def test_follow_yields_appended_events_then_stops(tmp_path):
    w = TrailWriter(tmp_path, RUN_ID)
    w.emit("run-start")
    w.emit("step-start", node="capture")
    w.emit("step-done", node="capture")

    seen: list[dict] = []

    def stop() -> bool:
        return len(seen) >= 3  # stop once we've drained the three existing events

    for ev in follow(tmp_path, poll_s=0.01, stop=stop):
        seen.append(ev)

    assert [ev["event"] for ev in seen] == ["run-start", "step-start", "step-done"]


def test_follow_resumes_from_since(tmp_path):
    w = TrailWriter(tmp_path, RUN_ID)
    w.emit("run-start")
    w.emit("step-start", node="capture")

    seen: list[dict] = []
    for ev in follow(tmp_path, since=1, poll_s=0.01, stop=lambda: len(seen) >= 1):
        seen.append(ev)

    assert [ev["seq"] for ev in seen] == [2]


def test_redactor_is_applied_to_the_serialized_line(tmp_path):
    def scrub(line: str) -> str:
        return line.replace("sk-secret-123", "∎REDACTED:BREASE_TOKEN∎")

    w = TrailWriter(tmp_path, RUN_ID, redactor=scrub)
    w.emit("guard-deny", data={"command": "curl -H 'Authorization: sk-secret-123'"})
    w.close()

    raw = (tmp_path / "trail.jsonl").read_text()
    assert "sk-secret-123" not in raw
    assert "∎REDACTED:BREASE_TOKEN∎" in raw


def _emit_then(tmp_path, event, **kw):
    w = TrailWriter(tmp_path, RUN_ID)
    w.emit(event, **kw)
    w.close()


def test_derive_status_gate_pending_reads_as_gate(tmp_path):
    _emit_then(tmp_path, "gate-pending", node="scope", data={"question": "which pages?"})
    st = derive_status(tmp_path)
    assert st.status == "gate"
    assert st.node == "scope"
    assert st.last_event["event"] == "gate-pending"


def test_derive_status_run_halt_reads_as_halted(tmp_path):
    _emit_then(tmp_path, "run-halt", node="build", data={"reason": "gate failed"})
    assert derive_status(tmp_path).status == "halted"


def test_derive_status_run_done_reads_as_done(tmp_path):
    _emit_then(tmp_path, "run-done", data={"exit_code": 0})
    assert derive_status(tmp_path).status == "done"


def test_derive_status_recent_step_reads_as_running(tmp_path):
    _emit_then(tmp_path, "step-start", node="capture")
    assert derive_status(tmp_path, heartbeat_grace_s=60).status == "running"


def test_derive_status_goes_stale_past_the_heartbeat_grace(tmp_path):
    # Hand-write an old heartbeat so recency, not event kind, decides.
    old = "2020-01-01T00:00:00.000Z"
    line = {
        "v": 1, "seq": 1, "at": old, "run_id": RUN_ID,
        "event": "heartbeat", "node": "capture", "attempt": None, "cycle": None, "data": {},
    }
    (tmp_path / "trail.jsonl").write_text(json.dumps(line) + "\n")
    st = derive_status(tmp_path, heartbeat_grace_s=60)
    assert st.status == "stale"
    assert st.node == "capture"


def test_derive_status_empty_trail_is_stale(tmp_path):
    st = derive_status(tmp_path)
    assert st.status == "stale"
    assert st.last_event is None
    assert st.node is None
