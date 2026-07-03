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
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cairn.kernel.sinks import JsonlSink, Sink

TRAIL_NAME = "trail.jsonl"
ENVELOPE_VERSION = 1

# Sink warnings go through `logging` (not the CLI's print-to-stderr idiom) deliberately: they
# fire from library code, possibly on daemon threads, where the caller owns the terminal. With
# no logging config, Python's lastResort handler still surfaces WARNING+ on stderr; an embedder
# can silence or route "cairn.*" loggers without cairn growing a logging config of its own.
_log = logging.getLogger("cairn.trail")

# The literal-match redaction marker (SECURITY.md §1.3). ``NAME`` is the declared secret's
# name, so a scrubbed line still says *which* secret sat there without ever showing its value.
REDACTION_MARKER = "∎REDACTED:{name}∎"


def make_redactor(secrets: Mapping[str, str]) -> Callable[[str], str] | None:
    """Build a literal-match scrubber over the *resolved* secret values (SECURITY.md §1.3).

    ``secrets`` maps declared name → resolved value; only truthy values are scrubbed (an empty
    value is skipped — a bare ``""`` would otherwise match everywhere). Returns ``None`` when
    nothing is scrubbable, so the caller pays *nothing* when no secret resolved. The returned
    callable replaces each value with ``∎REDACTED:NAME∎``; longest values first so one secret
    that is a substring of another never leaves the longer one half-scrubbed. A value that
    never appears costs one ``in`` scan — O(line length × num secrets) overall.
    """
    pairs = sorted(
        (
            (value, REDACTION_MARKER.format(name=name))
            for name, value in secrets.items()
            if value
        ),
        key=lambda p: len(p[0]),
        reverse=True,
    )
    if not pairs:
        return None

    def redact(text: str) -> str:
        for value, marker in pairs:
            if value in text:
                text = text.replace(value, marker)
        return text

    return redact


def _redact_obj(obj: object, redactor: Callable[[str], str]) -> object:
    """Apply ``redactor`` to every string in ``obj``, recursively (dicts/lists/strings).

    Redaction must run on the *values themselves*, before ``json.dumps``: a scrub of the
    serialized line would (a) miss a secret whose characters JSON escapes (``"``/``\\`` land
    as ``\\"``/``\\\\`` — the literal no longer matches, the secret ships recoverable) and
    (b) let a secret that resembles JSON syntax corrupt the authority line when substituted
    (invalid JSON → the reader drops it → ``_last_seq`` skips it → a resumed writer reuses
    the seq). Walking the object kills both failure modes structurally. Dict *keys* are
    scrubbed too — a secret has no business being a key, but it must not survive as one.
    """
    if isinstance(obj, str):
        return redactor(obj)
    if isinstance(obj, dict):
        return {
            (redactor(k) if isinstance(k, str) else k): _redact_obj(v, redactor)
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_redact_obj(v, redactor) for v in obj]
    return obj


def format_at(dt: datetime) -> str:
    """Format ``dt`` as the canonical trail/manifest timestamp.

    UTC, millisecond precision, Z-terminated — the one shape every ``at``/``created_at``
    in the system is written in, so trail events and run.json agree byte-for-byte. A naive
    ``dt`` is *read as* UTC (the codebase-wide tolerance: learnkit/gckit pin naive→UTC), so
    an aware clock round-trips exactly and a legacy naive one no longer needs downstream
    naive-pinning workarounds.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now_iso() -> str:
    """UTC, millisecond precision, Z-terminated — the envelope `at` format."""
    return format_at(datetime.now(timezone.utc))


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
    the offset strictly monotonic. Each emit is one flushed+fsynced append to sink #0, the
    authoritative :class:`~cairn.kernel.sinks.JsonlSink` (`trail.jsonl`), and — for any
    configured `tee_sinks` — a best-effort push of the same (redacted) event. Tee sinks are
    **never authority**: one that raises is logged and ignored, so a dead webhook can neither
    fail nor slow the run (OBSERVABILITY §2). Redaction (SECURITY §1.3) is applied here, once,
    to the envelope *object* before serialization — so the file, every tee, and emit's return
    value all carry the same scrubbed form (see :func:`_redact_obj` for why pre-serialization).
    """

    def __init__(
        self,
        run_dir: Path,
        run_id: str,
        *,
        redactor: Callable[[str], str] | None = None,
        tee_sinks: list[Sink] | None = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self._redactor = redactor
        self._tee_sinks = tee_sinks or []
        self._path = self.run_dir / TRAIL_NAME
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._seq = _last_seq(self._path)
        self._jsonl = JsonlSink(self._path)  # sink #0 — the authority

    def emit(
        self,
        event: str,
        node: str | None = None,
        attempt: int | None = None,
        cycle: int | None = None,
        data: dict | None = None,
    ) -> dict:
        """Append one envelope line; return the **redacted** envelope dict — exactly what
        landed on disk and what every tee sink received, never the raw values."""
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
        # Redact the OBJECT, then serialize (see _redact_obj for why order matters): the
        # authority line can never be corrupted by a JSON-syntax-shaped secret, an escaped
        # secret can never slip through, and the tee gets the same redacted dict directly.
        if self._redactor is not None:
            envelope = _redact_obj(envelope, self._redactor)
        # Authority first: an IO failure here is fatal (the run's record of truth).
        self._jsonl.write_line(json.dumps(envelope, ensure_ascii=False))
        for sink in self._tee_sinks:
            try:
                sink.emit(envelope)
            except Exception:  # noqa: BLE001 — a tee must never fail the run
                _log.warning(
                    "cairn: trail sink %r raised on emit — ignored.",
                    getattr(sink, "_name", type(sink).__name__),
                )
        return envelope

    def close(self) -> None:
        self._jsonl.close()
        for sink in self._tee_sinks:
            try:
                sink.close()
            except Exception:  # noqa: BLE001 — closing a tee must never fail the run
                _log.warning("cairn: trail sink close raised — ignored.")

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
