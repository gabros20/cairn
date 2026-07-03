"""Trail sinks — the tee layer (OBSERVABILITY.md §2).

`trail.jsonl` is sink #0, the authority (tested in test_trail.py). These cover the *tee*
contract: a webhook sink pushes each event as JSON, never blocks/fails/slows the run, retries
a bounded number of times, drops on queue overflow with one legible warning, and never leaks a
secret value into that warning. Plus `build_tee_sinks` wiring from the `[sinks]` config table.
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from cairn.kernel.sinks import JsonlSink, WebhookSink, build_tee_sinks


# --------------------------------------------------------------------------- #
# JsonlSink — sink #0, the authority (byte-exact append).
# --------------------------------------------------------------------------- #


def test_jsonl_sink_appends_lines_verbatim(tmp_path):
    sink = JsonlSink(tmp_path / "trail.jsonl")
    sink.write_line('{"seq":1}')
    sink.write_line('{"seq":2}')
    sink.close()
    assert (tmp_path / "trail.jsonl").read_text() == '{"seq":1}\n{"seq":2}\n'


def test_jsonl_sink_creates_missing_parent(tmp_path):
    JsonlSink(tmp_path / "runs" / "r" / "trail.jsonl").close()
    assert (tmp_path / "runs" / "r").is_dir()


# --------------------------------------------------------------------------- #
# WebhookSink — the tee.
# --------------------------------------------------------------------------- #


def _drain(sink: WebhookSink, timeout: float = 2.0) -> None:
    """Wait until the sink's queue has been fully worked, then stop it."""
    deadline = time.monotonic() + timeout
    while not sink._queue.empty() and time.monotonic() < deadline:
        time.sleep(0.01)
    sink.close()


def test_webhook_posts_each_event_as_json(tmp_path):
    posted: list[bytes] = []
    sink = WebhookSink("hook", "https://ops.invalid/x", poster=lambda url, body, t: posted.append((url, body)))
    sink.emit({"seq": 1, "event": "run-start", "data": {"x": 1}})
    _drain(sink)

    assert len(posted) == 1
    url, body = posted[0]
    assert url == "https://ops.invalid/x"
    import json
    assert json.loads(body) == {"seq": 1, "event": "run-start", "data": {"x": 1}}


def test_webhook_failure_never_raises_out_of_emit(tmp_path):
    def boom(url, body, t):
        raise ConnectionError("dead endpoint")

    sink = WebhookSink("hook", "https://ops.invalid/x", poster=boom, retries=1)
    # emit must return normally — a dead webhook cannot propagate into the walker.
    sink.emit({"seq": 1, "event": "run-start"})
    _drain(sink)  # worker also swallows; close returns cleanly


def test_webhook_retries_a_bounded_number_of_times_then_warns(caplog):
    attempts = {"n": 0}

    def flaky(url, body, t):
        attempts["n"] += 1
        raise TimeoutError("nope")

    with caplog.at_level(logging.WARNING, logger="cairn.sinks"):
        sink = WebhookSink("hook", "https://ops.invalid/x", poster=flaky, retries=2)
        sink.emit({"seq": 1, "event": "run-start"})
        _drain(sink)

    assert attempts["n"] == 3  # 1 initial + 2 retries, bounded
    assert any("failed" in r.message.lower() for r in caplog.records)


def test_webhook_drops_on_queue_overflow_with_one_warning(caplog):
    release = threading.Event()

    def slow(url, body, t):
        release.wait(2.0)  # block the worker so the queue backs up

    with caplog.at_level(logging.WARNING, logger="cairn.sinks"):
        sink = WebhookSink("hook", "https://ops.invalid/x", poster=slow, queue_max=1)
        for seq in range(20):
            sink.emit({"seq": seq, "event": "heartbeat"})  # far more than the queue holds
        release.set()
        sink.close()

    warnings = [r for r in caplog.records if "full" in r.message.lower() or "drop" in r.message.lower()]
    assert len(warnings) == 1  # exactly one legible warning, not one per drop


def test_webhook_warning_never_leaks_the_event_or_a_secret(caplog):
    def boom(url, body, t):
        raise ConnectionError("refused")

    with caplog.at_level(logging.WARNING, logger="cairn.sinks"):
        sink = WebhookSink("hook", "https://ops.invalid/x", poster=boom, retries=0)
        sink.emit({"seq": 1, "event": "guard-deny", "data": {"secret": "sk-live-DEADBEEF"}})
        _drain(sink)

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "sk-live-DEADBEEF" not in joined  # the payload is never echoed into a log line


def test_webhook_event_filter_globs(tmp_path):
    posted: list = []
    sink = WebhookSink(
        "hook", "https://ops.invalid/x",
        events=["run-*", "gate-pending"],
        poster=lambda url, body, t: posted.append(body),
    )
    sink.emit({"seq": 1, "event": "run-start"})
    sink.emit({"seq": 2, "event": "heartbeat"})  # filtered out
    sink.emit({"seq": 3, "event": "gate-pending"})
    _drain(sink)

    import json
    events = [json.loads(b)["event"] for b in posted]
    assert events == ["run-start", "gate-pending"]


def test_webhook_close_is_idempotent_and_bounded():
    sink = WebhookSink("hook", "https://ops.invalid/x", poster=lambda *a: None)
    sink.close()
    sink.close()  # second close must not hang or raise


# --------------------------------------------------------------------------- #
# build_tee_sinks — wiring from [sinks] config.
# --------------------------------------------------------------------------- #


def test_build_skips_the_jsonl_authority_sink():
    # jsonl is sink #0 (the trail file itself), handled by TrailWriter — never a tee.
    sinks = build_tee_sinks({"jsonl": {}})
    assert sinks == []


def test_build_constructs_a_webhook_sink():
    sinks = build_tee_sinks({"webhook": {"url": "https://ops.invalid/cairn", "events": ["run-*"]}})
    assert len(sinks) == 1 and isinstance(sinks[0], WebhookSink)
    for s in sinks:
        s.close()


def test_build_warns_and_skips_a_webhook_missing_its_url(caplog):
    with caplog.at_level(logging.WARNING, logger="cairn.sinks"):
        sinks = build_tee_sinks({"webhook": {"events": ["run-*"]}})
    assert sinks == []
    assert any("url" in r.getMessage().lower() for r in caplog.records)


def test_build_warns_and_skips_an_unknown_sink_type(caplog):
    with caplog.at_level(logging.WARNING, logger="cairn.sinks"):
        sinks = build_tee_sinks({"otel": {"endpoint": "x"}})
    assert sinks == []
    assert any("otel" in r.getMessage() for r in caplog.records)
