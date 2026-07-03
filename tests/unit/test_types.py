"""Pinned kernel contracts: exit codes, tier/effort enums, dataclass shapes."""

from __future__ import annotations

import dataclasses

from cairn.kernel.types import (
    EFFORTS,
    TIERS,
    Capabilities,
    ExitCode,
    Finding,
    Invocation,
    Result,
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
    assert EFFORTS == ("low", "medium", "high", "xhigh")


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
