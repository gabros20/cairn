"""Pinned kernel contracts: exit codes, tier/effort enums, dataclass shapes."""

from __future__ import annotations

import dataclasses

import pytest

from cairn.kernel.types import (
    EFFORTS,
    TIERS,
    Capabilities,
    ExitCode,
    Finding,
    Invocation,
    OutcomeClass,
    Result,
    RunOutcome,
    _WAITING_KINDS,
    classify_exit,
)


def test_exit_codes_match_architecture_taxonomy():
    assert ExitCode.OK == 0
    assert ExitCode.CONFIG == 2
    assert ExitCode.GATE_FAILED == 3
    assert ExitCode.EXECUTOR == 4
    assert ExitCode.TIMEOUT == 5
    assert ExitCode.NEEDS_HUMAN == 6
    assert ExitCode.BUDGET == 7


def test_exit_code_is_int_usable_as_process_code():
    # IntEnum members must behave as plain ints for sys.exit / subprocess codes.
    assert int(ExitCode.GATE_FAILED) == 3
    assert ExitCode.OK + 0 == 0


def test_tiers_and_efforts_pinned():
    assert TIERS == ("reasoning", "balanced", "cheap")
    # W4 (claude-F11): "max" added — the claude CLI accepts it and codex/grok map it natively.
    assert EFFORTS == ("low", "medium", "high", "xhigh", "max")


def test_result_usage_defaults_to_none():
    # The stable plumbing: usage is optional and absent by default (plain-text executors),
    # so every existing Result(...) call site keeps None without change.
    r = Result(step={"status": "done"}, exit_code=0, duration_s=1.0)
    assert r.usage is None
    r2 = Result(step=None, exit_code=0, duration_s=1.0, usage={"in_tokens": 5})
    assert r2.usage == {"in_tokens": 5}


def test_finding_defaults_fix_to_none():
    f = Finding(level="error", message="boom")
    assert f.fix is None
    f2 = Finding(level="warning", message="off-version", fix="pin 0.138")
    assert f2.fix == "pin 0.138"


def test_exit_codes_capacity_and_blocked():
    assert ExitCode.CAPACITY == 8
    assert ExitCode.BLOCKED == 9


def test_classify_exit_done():
    o = classify_exit(0)
    assert o == RunOutcome(outcome=OutcomeClass.DONE)
    assert o.waiting_kind is None


def test_classify_exit_waiting_kinds():
    assert classify_exit(6) == RunOutcome(
        outcome=OutcomeClass.WAITING, waiting_kind="needs_human"
    )
    assert classify_exit(8) == RunOutcome(
        outcome=OutcomeClass.WAITING, waiting_kind="capacity"
    )
    assert classify_exit(9) == RunOutcome(
        outcome=OutcomeClass.WAITING, waiting_kind="blocked"
    )


def test_classify_exit_failed_known_unknown_and_negative():
    # Known failure (4), unknown positive (77), signal death (-9) — all FAILED.
    assert classify_exit(4) == RunOutcome(outcome=OutcomeClass.FAILED)
    assert classify_exit(77) == RunOutcome(outcome=OutcomeClass.FAILED)
    assert classify_exit(-9) == RunOutcome(outcome=OutcomeClass.FAILED)
    assert classify_exit(4).waiting_kind is None


def test_run_outcome_frozen_and_equality():
    a = RunOutcome(outcome=OutcomeClass.WAITING, waiting_kind="capacity")
    b = RunOutcome(outcome=OutcomeClass.WAITING, waiting_kind="capacity")
    assert a == b
    try:
        a.outcome = OutcomeClass.DONE  # type: ignore[misc]
        raise AssertionError("expected FrozenInstanceError")
    except dataclasses.FrozenInstanceError:
        pass


def test_run_outcome_rejects_waiting_without_kind():
    with pytest.raises(ValueError, match="WAITING requires a waiting_kind"):
        RunOutcome(outcome=OutcomeClass.WAITING, waiting_kind=None)


def test_run_outcome_rejects_kind_on_non_waiting():
    with pytest.raises(ValueError, match="waiting_kind=None"):
        RunOutcome(outcome=OutcomeClass.DONE, waiting_kind="capacity")
    with pytest.raises(ValueError, match="waiting_kind=None"):
        RunOutcome(outcome=OutcomeClass.FAILED, waiting_kind="blocked")


def test_waiting_kinds_map_covers_waiting_exit_codes():
    # D8 waiting-class ExitCode members must all appear in _WAITING_KINDS.
    expected = {ExitCode.NEEDS_HUMAN, ExitCode.CAPACITY, ExitCode.BLOCKED}
    assert set(_WAITING_KINDS) == {int(c) for c in expected}
    for code in ExitCode:
        o = classify_exit(int(code))
        if code in expected:
            assert o.outcome is OutcomeClass.WAITING
            assert o.waiting_kind == _WAITING_KINDS[int(code)]
        elif code is ExitCode.OK:
            assert o.outcome is OutcomeClass.DONE
        else:
            assert o.outcome is OutcomeClass.FAILED
            assert o.waiting_kind is None
