"""The Trail Protocol v1 — one append-only event stream per run.

`runs/<id>/trail.jsonl` is the run's public contract (OBSERVABILITY.md §1): a versioned
envelope per line, a strictly monotonic `seq` the consumer uses as its offset, atomic
flushed appends so a reader never treats a torn line as final. Single writer — only the
walker appends; this module is that writer plus the readers built on the same guarantees.

No servers, no database, stdlib only. `follow` is a polling generator the caller drives;
there are no threads or daemons here.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

TRAIL_NAME = "trail.jsonl"
ENVELOPE_VERSION = 1


def _now_iso() -> str:
    """UTC, millisecond precision, Z-terminated — the envelope `at` format."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_at(at: str) -> datetime:
    return datetime.fromisoformat(at.replace("Z", "+00:00"))


def _last_seq(path: Path) -> int:
    """Highest seq already on disk (0 if none). Tolerates a torn/partial final line."""
    if not path.exists():
        return 0
    last = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                last = json.loads(line)["seq"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue  # torn/partial line — skip it, never crash the writer
    return last


class TrailWriter:
    """Single-writer appender for one run's trail.

    `seq` resumes from the last line on re-open, so a crashed-then-resumed walker keeps
    the offset strictly monotonic. Each emit is one flushed+fsynced append.
    """

    def __init__(
        self,
        run_dir: Path,
        run_id: str,
        *,
        redactor: Callable[[str], str] | None = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self._redactor = redactor
        self._path = self.run_dir / TRAIL_NAME
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._seq = _last_seq(self._path)
        self._fh = self._path.open("a", encoding="utf-8")

    def emit(
        self,
        event: str,
        node: str | None = None,
        attempt: int | None = None,
        cycle: int | None = None,
        data: dict | None = None,
    ) -> dict:
        """Append one envelope line; return the event dict that was written."""
        self._seq += 1
        envelope = {
            "v": ENVELOPE_VERSION,
            "seq": self._seq,
            "at": _now_iso(),
            "run_id": self.run_id,
            "event": event,
            "node": node,
            "attempt": attempt,
            "cycle": cycle,
            "data": data if data is not None else {},
        }
        serialized = json.dumps(envelope, ensure_ascii=False)
        if self._redactor is not None:
            serialized = self._redactor(serialized)
        self._fh.write(serialized + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        return envelope

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> TrailWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_trail(run_dir: Path, since: int | None = None) -> Iterator[dict]:
    """Yield parsed events with seq > since, tolerating a torn/partial final line."""
    path = Path(run_dir) / TRAIL_NAME
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn/partial line (realistically only the final one)
            if since is not None and ev.get("seq", 0) <= since:
                continue
            yield ev


def follow(
    run_dir: Path,
    since: int | None = None,
    poll_s: float = 0.5,
    stop: Callable[[], bool] | None = None,
) -> Iterator[dict]:
    """tail -f the trail by polling file growth: yield new events as they appear.

    A pure generator — the caller drives it. Between drains it checks `stop()`; when that
    returns True the generator returns. With `stop=None` it follows indefinitely.
    """
    last_seq = since
    while True:
        for ev in read_trail(run_dir, since=last_seq):
            last_seq = ev.get("seq", last_seq)
            yield ev
        if stop is not None and stop():
            return
        time.sleep(poll_s)


@dataclass(frozen=True)
class RunStatus:
    """The `cairn ps` view of one run (OBSERVABILITY.md §3)."""

    status: str  # running | gate | halted | done | stale
    last_event: dict | None
    node: str | None


def derive_status(run_dir: Path, *, heartbeat_grace_s: int | None = None) -> RunStatus:
    """Decide a run's live status from its trail's last event.

    The last event kind decides: gate-pending → gate, run-halt → halted, run-done → done.
    Otherwise the run looks alive (heartbeat / step-* / etc.) and recency governs: within
    `heartbeat_grace_s` → running, past it → stale. With no grace given, a live-looking
    last event is reported running (no crash detection).
    """
    last: dict | None = None
    for ev in read_trail(run_dir):
        last = ev

    if last is None:
        return RunStatus(status="stale", last_event=None, node=None)

    node = last.get("node")
    event = last.get("event")

    if event == "gate-pending":
        return RunStatus(status="gate", last_event=last, node=node)
    if event == "run-halt":
        return RunStatus(status="halted", last_event=last, node=node)
    if event == "run-done":
        return RunStatus(status="done", last_event=last, node=node)

    if heartbeat_grace_s is not None:
        try:
            age_s = (datetime.now(timezone.utc) - _parse_at(last["at"])).total_seconds()
        except (KeyError, ValueError):
            age_s = None
        if age_s is not None and age_s > heartbeat_grace_s:
            return RunStatus(status="stale", last_event=last, node=node)

    return RunStatus(status="running", last_event=last, node=node)
