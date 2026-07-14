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
    # A VALID (MAC-signed) decision is what makes a gate resumable; write it via the real
    # external-answer writer so the file carries a mac resolve_gate accepts. (An unsigned
    # hand-written file is now REJECTED — see the panel-findings tamper tests.)
    answer_gate(tmp_path, "tone", "formal")
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
    assert re.fullmatch(r"[0-9a-f]{64}", payload.pop("mac"))  # HMAC-SHA256 hex (gatekeys)
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


# --------------------------------------------------------------------------- #
# Authentication — the decision file is HMAC-signed; a forged/tampered one is rejected.
# (W2-gate, codex-F1). The hermetic XDG_STATE_HOME redirect is autouse in conftest.py.
# --------------------------------------------------------------------------- #


def _events(rec: _Recorder) -> list[str]:
    return [e for e, _ in rec.events]


def test_operator_roundtrip_answer_then_resolve_is_honored_without_tamper(tmp_path: Path) -> None:
    """The operator pattern still works: answer_gate writes a MAC resolve_gate accepts."""
    answer_gate(tmp_path, "tone", "formal")
    rec = _Recorder()

    choice = resolve_gate(
        _gate(), tmp_path, interactive=False, presets={}, emit=rec, now=NOW
    )

    assert choice == "formal"
    assert "gate-tamper" not in _events(rec)  # a legitimate signed decision is trusted


def test_tampered_choice_with_stale_mac_is_rejected(tmp_path: Path) -> None:
    """Edit `choice` on disk (leaving the old MAC) → rejected + gate-tamper, forge not honored."""
    answer_gate(tmp_path, "tone", "formal")
    gp = gate_path(tmp_path, "tone")
    doc = json.loads(gp.read_text(encoding="utf-8"))
    doc["choice"] = "friendly"  # flip the decision but keep the MAC that signed "formal"
    gp.write_text(json.dumps(doc), encoding="utf-8")
    rec = _Recorder()

    with pytest.raises(GateNeedsHuman):  # headless, no default → fail SAFE, never auto-pass
        resolve_gate(_gate(default=""), tmp_path, interactive=False, presets={}, emit=rec, now=NOW)

    assert "gate-tamper" in _events(rec)


def test_cross_gate_replay_is_rejected_name_is_in_the_mac(tmp_path: Path) -> None:
    """A valid decision file for gate A copied to gate B's path is rejected (name is signed)."""
    answer_gate(tmp_path, "alpha", "formal")
    src = gate_path(tmp_path, "alpha")
    dst = gate_path(tmp_path, "beta")
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")  # replay A's signed file at B
    rec = _Recorder()

    with pytest.raises(GateNeedsHuman):
        resolve_gate(
            _gate(name="beta", default=""), tmp_path, interactive=False, presets={}, emit=rec, now=NOW
        )

    reasons = [d["data"].get("reason") for e, d in rec.events if e == "gate-tamper"]
    assert reasons == ["mac-mismatch"]  # verified with gate="beta", signed with gate="alpha"


def test_valid_mac_but_choice_off_menu_is_rejected(tmp_path: Path) -> None:
    """Defense in depth: a properly-signed decision whose choice isn't an option is rejected."""
    from cairn.kernel.gatekeys import compute_mac, ensure_run_key

    secret = ensure_run_key(tmp_path)
    at = "2026-07-03T11:04:00.000Z"
    gp = gate_path(tmp_path, "tone")
    gp.parent.mkdir(parents=True, exist_ok=True)
    gp.write_text(
        json.dumps(
            {"choice": "sneaky", "by": "external", "at": at,
             "mac": compute_mac(secret, tmp_path, "tone", "sneaky", "external", at)}
        ),
        encoding="utf-8",
    )
    rec = _Recorder()

    with pytest.raises(GateNeedsHuman):
        resolve_gate(_gate(default=""), tmp_path, interactive=False, presets={}, emit=rec, now=NOW)

    reasons = [d["data"].get("reason") for e, d in rec.events if e == "gate-tamper"]
    assert reasons == ["choice-not-an-option"]


def test_missing_secret_is_tamper_never_auto_passed(tmp_path: Path) -> None:
    """A validly-signed file whose key has vanished is treated as tamper, never trusted."""
    from cairn.kernel.gatekeys import gate_keys_dir

    answer_gate(tmp_path, "tone", "formal")  # writes a genuinely valid MAC
    for key in gate_keys_dir().glob("*.key"):  # ...then the secret is gone
        key.unlink()
    rec = _Recorder()

    with pytest.raises(GateNeedsHuman):  # fail SAFE — no secret means cannot authenticate
        resolve_gate(_gate(default=""), tmp_path, interactive=False, presets={}, emit=rec, now=NOW)

    reasons = [d["data"].get("reason") for e, d in rec.events if e == "gate-tamper"]
    assert reasons == ["missing-secret"]


def test_same_run_id_in_different_dirs_get_distinct_keys_and_dont_cross_verify(tmp_path: Path) -> None:
    """F-SEC-2: two runs sharing a run_id in different workspaces must not share a key, and a
    decision signed in one must not verify in the other (run identity is bound into key + MAC)."""
    from cairn.kernel.gatekeys import ensure_run_key

    a = tmp_path / "wsA" / "t-20260703"
    b = tmp_path / "wsB" / "t-20260703"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert a.name == b.name  # identical run_id, different absolute dirs

    assert ensure_run_key(a) != ensure_run_key(b)  # distinct per-run secrets

    answer_gate(a, "tone", "formal")  # a legitimately-signed decision in run A
    bp = gate_path(b, "tone")
    bp.parent.mkdir(parents=True, exist_ok=True)
    bp.write_text(gate_path(a, "tone").read_text(encoding="utf-8"), encoding="utf-8")  # replay at B
    rec = _Recorder()

    with pytest.raises(GateNeedsHuman):  # B verifies with B's secret + B's run-disc → rejected
        resolve_gate(_gate(default=""), b, interactive=False, presets={}, emit=rec, now=NOW)

    reasons = [d["data"].get("reason") for e, d in rec.events if e == "gate-tamper"]
    assert reasons == ["mac-mismatch"]
