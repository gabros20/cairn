"""Shared unit-test fixtures.

``_hermetic_gate_key_state`` (autouse) redirects the per-run gate-key state dir
(``gatekeys.gate_keys_dir`` honors ``XDG_STATE_HOME``) into a fresh tmp dir for EVERY test,
including subprocesses that inherit the env. Gate commits mint a key on write, so without
this every gate-touching test would scribble into the real ``~/.local/state/cairn``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _hermetic_gate_key_state(tmp_path_factory, monkeypatch):
    state = tmp_path_factory.mktemp("xdg-state")
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    yield
