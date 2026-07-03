"""Trail sinks — the tee layer (OBSERVABILITY.md §2).

One event stream, many consumers. Sink #0 is `trail.jsonl` itself — the **authority**:
:class:`JsonlSink` appends each (already-serialized, already-redacted) line and fsyncs it,
and a failure there is fatal by contract (it is the run's source of truth). Every other sink
is a **tee — never authority**: fire-and-forget with bounded retry, so a dead endpoint can
never slow, block, or fail a run (OBSERVABILITY §2, the Nextflow-weblog move).

Built-ins: `jsonl` (sink #0, owned by :class:`~cairn.kernel.trail.TrailWriter`) and `webhook`
(the shipped push target). OTel/Slack are post-C7 plugins registered via the ``cairn.sinks``
entry point (API.md §6) — not built here. Stdlib only: ``urllib`` for the POST, a bounded
``queue`` + one daemon thread per webhook so emit never blocks the walker.

Warnings here go through ``logging`` (logger ``cairn.sinks``) rather than the CLI's
print-to-stderr idiom — a deliberate library-layer choice: sink failures surface from daemon
threads inside kernel code where the caller owns the terminal. With no logging config, Python's
lastResort handler still prints WARNING+ to stderr; an embedder can silence or route ``cairn.*``.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import queue
import threading
import urllib.request
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

log = logging.getLogger("cairn.sinks")

# Tee defaults — deliberately conservative: a short timeout and a couple of retries so a slow
# endpoint is abandoned quickly, and a queue large enough to absorb a burst of a normal run's
# tens-of-events without ever growing unboundedly.
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_RETRIES = 2  # total attempts = 1 + retries
DEFAULT_QUEUE_MAX = 1000


@runtime_checkable
class Sink(Protocol):
    """A trail-event consumer. ``emit`` must never block or raise — a tee is best-effort."""

    def emit(self, event: dict) -> None: ...

    def close(self) -> None: ...


class JsonlSink:
    """Sink #0 — the authoritative append-only trail file (OBSERVABILITY §1–2).

    Unlike a tee, this is the run's source of truth: each line is appended, flushed, and
    fsynced, and an IO error here *propagates* (the walker must halt if it can no longer
    record the trail). :class:`~cairn.kernel.trail.TrailWriter` owns one of these and does the
    seq/serialize/redact work, so this class stays a dumb byte-exact line appender.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def write_line(self, serialized: str) -> None:
        self._fh.write(serialized + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()


class WebhookSink:
    """POST each event as JSON to a URL — a tee that can never affect the run.

    ``emit`` only enqueues (non-blocking); a single daemon worker drains the queue and POSTs
    with a short timeout and bounded retry. Failure modes, all silent to the walker:

    - **endpoint down / slow** → bounded retries, then ONE legible warning; the event is
      dropped and the run is unaffected.
    - **queue overflow** (worker not keeping up) → the event is dropped and ONE warning is
      logged (not one per drop). The queue is bounded so a wedged endpoint cannot leak memory.

    Warnings carry the sink name, the URL, and the failure *type* — never the event payload,
    so a redacted-or-not secret in an event body can never reach a warning line.
    """

    def __init__(
        self,
        name: str,
        url: str,
        *,
        events: list[str] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        retries: int = DEFAULT_RETRIES,
        queue_max: int = DEFAULT_QUEUE_MAX,
        poster: Callable[[str, bytes, float], None] | None = None,
    ) -> None:
        self._name = name
        self._url = url
        self._patterns = list(events) if events else None
        self._timeout_s = timeout_s
        self._retries = retries
        self._poster = poster or self._http_post
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, queue_max))
        self._stop = threading.Event()
        self._warned_full = False
        self._thread = threading.Thread(
            target=self._worker, name=f"cairn-sink-{name}", daemon=True
        )
        self._thread.start()

    def _matches(self, event_name: str) -> bool:
        if self._patterns is None:
            return True
        return any(fnmatch.fnmatchcase(event_name, p) for p in self._patterns)

    def emit(self, event: dict) -> None:
        if not self._matches(str(event.get("event", ""))):
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            if not self._warned_full:
                self._warned_full = True
                log.warning(
                    "cairn sink %r: webhook queue full (>%d pending) — dropping events; "
                    "POST to %s is not keeping up. The run is unaffected.",
                    self._name, self._queue.maxsize, self._url,
                )

    def _worker(self) -> None:
        while True:
            try:
                event = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                self._deliver(event)
            finally:
                self._queue.task_done()

    def _deliver(self, event: dict) -> None:
        body = json.dumps(event, ensure_ascii=False).encode("utf-8")
        last_exc: Exception | None = None
        for _attempt in range(self._retries + 1):
            try:
                self._poster(self._url, body, self._timeout_s)
                return
            except Exception as exc:  # noqa: BLE001 — a tee must never propagate
                last_exc = exc
        # Bounded retry exhausted. Log the failure TYPE only (never str(exc) — an endpoint's
        # error text could echo the posted body) and never the event itself.
        log.warning(
            "cairn sink %r: webhook POST to %s failed after %d attempt(s) (%s) — event "
            "dropped. The run is unaffected.",
            self._name, self._url, self._retries + 1,
            type(last_exc).__name__ if last_exc else "unknown",
        )

    @staticmethod
    def _http_post(url: str, body: bytes, timeout_s: float) -> None:
        req = urllib.request.Request(
            url, data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 — configured URL
            resp.read()

    def flush(self) -> None:
        """Block until every enqueued event has been fully worked (delivered or dropped).

        The public settle point — ``queue.join()`` over the worker's ``task_done()`` contract,
        so it returns only after in-flight deliveries finish, not merely when the queue looks
        empty. For tests and orderly shutdown; the walker itself never calls it (a tee must
        never make the run wait).
        """
        self._queue.join()

    def close(self, timeout_s: float = 2.0) -> None:
        """Signal the worker to finish draining, then join (bounded). Idempotent."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout_s)


def build_tee_sinks(config_sinks: dict[str, Any] | None) -> list[Sink]:
    """Construct the tee sinks declared in ``[sinks]`` (OBSERVABILITY §2).

    ``jsonl`` is sink #0 (the authority) and is skipped here — TrailWriter owns it. ``webhook``
    builds a :class:`WebhookSink`. A malformed spec or an unknown sink type is a **warning, not
    an error**: the sink is skipped so a bad `[sinks]` block can never break a run, doctor, or
    plan. Post-C7 plugin types (otel/slack) arrive via the ``cairn.sinks`` entry point.
    """
    sinks: list[Sink] = []
    for name, spec in (config_sinks or {}).items():
        if name == "jsonl":
            continue  # sink #0 — the authoritative trail file, always on (TrailWriter).
        if name != "webhook":
            log.warning(
                "cairn sink %r: unknown sink type — ignored (built-ins: jsonl, webhook; "
                "otel/slack are plugins).", name,
            )
            continue
        url = spec.get("url")
        if not isinstance(url, str) or not url:
            log.warning("cairn sink %r: missing or invalid 'url' — sink ignored.", name)
            continue
        events = spec.get("events")
        if events is not None and not (
            isinstance(events, list) and all(isinstance(e, str) for e in events)
        ):
            log.warning(
                "cairn sink %r: 'events' must be a list of event globs — ignoring the filter.",
                name,
            )
            events = None
        sinks.append(WebhookSink(name, url, events=events))
    return sinks
