"""Gate resolution — the resumable human decision point (ARCHITECTURE §3.2).

Behaviour tests against ``cairn.kernel.gatekit`` through its public surface: a real run
dir on disk and GateNodes constructed directly. Each test asserts one observable fact —
the recorded decision file's ``choice``/``by``, the emitted trail events, the TTY
re-prompt loop, the headless-no-default halt, and the external answer path.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from cairn.kernel.gatekit import (
    GateNeedsHuman,
    answer_gate,
    gate_path,
    is_answered,
    resolve_gate,
)
from cairn.kernel.plan import GateNode

NOW = datetime(2026, 7, 3, 11, 4)


def _gate(name: str = "tone", default: str = "friendly") -> GateNode:
    return GateNode(
        name=name,
        reads=("greeting",),
        ask="What tone should the message use?",
        options=(("friendly", "Warm and casual"), ("formal", "Polished and professional")),
        default=default,
        when_runtime=None,
    )


class _Recorder:
    """A stand-in for the walker's locked trail emitter."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event: str, *, node=None, data=None):
        self.events.append((event, {"node": node, "data": data or {}}))
        return {"event": event}


def test_already_answered_gate_is_skipped_without_emitting(tmp_path: Path) -> None:
    gp = gate_path(tmp_path, "tone")
    gp.parent.mkdir(parents=True)
    gp.write_text(json.dumps({"choice": "formal", "by": "tty"}), encoding="utf-8")
    rec = _Recorder()

    choice = resolve_gate(
        _gate(), tmp_path, interactive=True, presets={}, emit=rec, now=NOW
    )

    assert choice == "formal"
    assert rec.events == []  # never re-asked, never re-emitted


def test_preset_writes_by_flag_and_emits_answered(tmp_path: Path) -> None:
    rec = _Recorder()

    choice = resolve_gate(
        _gate(), tmp_path, interactive=False, presets={"tone": "formal"}, emit=rec, now=NOW
    )

    assert choice == "formal"
    payload = json.loads(gate_path(tmp_path, "tone").read_text(encoding="utf-8"))
    # `at` is trail.format_at's canonical shape (UTC, ms, Z) — a naive `now` (the legacy
    # clock) is read as UTC, so pre-fix callers still produce the one system-wide shape.
    assert payload == {"choice": "formal", "by": "flag", "at": "2026-07-03T11:04:00.000Z"}
    assert [e for e, _ in rec.events] == ["gate-answered"]


def test_preset_not_an_option_is_a_config_error(tmp_path: Path) -> None:
    from cairn.kernel.errors import ConfigError

    with pytest.raises(ConfigError):
        resolve_gate(
            _gate(), tmp_path, interactive=False, presets={"tone": "nope"}, emit=_Recorder(), now=NOW
        )


def test_interactive_reprompts_until_valid_then_records_by_tty(tmp_path: Path) -> None:
    answers = iter(["bogus", "formal"])
    rec = _Recorder()

    choice = resolve_gate(
        _gate(),
        tmp_path,
        interactive=True,
        presets={},
        emit=rec,
        now=NOW,
        prompt=lambda _p: next(answers),
        out=open("/dev/null", "w"),
    )

    assert choice == "formal"
    payload = json.loads(gate_path(tmp_path, "tone").read_text(encoding="utf-8"))
    assert payload["by"] == "tty"
    assert [e for e, _ in rec.events] == ["gate-pending", "gate-answered"]


def test_headless_with_default_writes_by_default(tmp_path: Path) -> None:
    rec = _Recorder()

    choice = resolve_gate(
        _gate(default="friendly"), tmp_path, interactive=False, presets={}, emit=rec, now=NOW
    )

    assert choice == "friendly"
    assert json.loads(gate_path(tmp_path, "tone").read_text(encoding="utf-8"))["by"] == "default"
    assert [e for e, _ in rec.events] == ["gate-answered"]


def test_headless_without_default_emits_pending_then_raises(tmp_path: Path) -> None:
    rec = _Recorder()

    with pytest.raises(GateNeedsHuman):
        resolve_gate(
            _gate(default=""), tmp_path, interactive=False, presets={}, emit=rec, now=NOW
        )

    assert [e for e, _ in rec.events] == ["gate-pending"]
    assert not is_answered(tmp_path, "tone")  # nothing written


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
def test_interactive_tty_interrupt_becomes_needs_human(tmp_path: Path, exc) -> None:
    rec = _Recorder()

    def interrupt(_prompt):
        raise exc()

    with pytest.raises(GateNeedsHuman):
        resolve_gate(
            _gate(), tmp_path, interactive=True, presets={}, emit=rec, now=NOW,
            prompt=interrupt, out=open("/dev/null", "w"),
        )

    assert [e for e, _ in rec.events] == ["gate-pending"]  # emitted before the interrupt
    assert not is_answered(tmp_path, "tone")               # nothing recorded


def test_answer_gate_writes_by_external(tmp_path: Path) -> None:
    answer_gate(tmp_path, "tone", "formal")

    payload = json.loads(gate_path(tmp_path, "tone").read_text(encoding="utf-8"))
    assert payload["choice"] == "formal"
    assert payload["by"] == "external"
    # Canonical trail.format_at shape — UTC, millisecond precision, Z-terminated.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", payload["at"])
    assert is_answered(tmp_path, "tone")
