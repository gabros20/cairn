"""Executor authors import their contract surface from cairn.executors.base."""

from __future__ import annotations

from cairn.executors import base
from cairn.kernel import types


def test_base_reexports_the_protocol_surface():
    for name in ("Executor", "Capabilities", "Invocation", "Result", "Finding"):
        assert getattr(base, name) is getattr(types, name)


def test_base_reexports_tier_and_effort_enums():
    assert base.TIERS == types.TIERS
    assert base.EFFORTS == types.EFFORTS
