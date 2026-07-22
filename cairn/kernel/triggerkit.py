"""triggerkit — compatibility facade over the W0.5 three-way split.

The implementation lives in three modules (docs/FACTORY-PLAN.md §3 W0.5 / D10):

- :mod:`cairn.kernel.queue_ledger` — claim/ledger mechanics (pure state)
- :mod:`cairn.kernel.queue_drain` — drain loop (process orchestration)
- :mod:`cairn.kernel.trigger_host` — declaration + host-watcher integration

This module re-exports the public surface so every existing importer
(``cli.py``, tests) keeps working unchanged. New code may import from the
home modules directly.

``run_trigger`` looks up ``claim``/``consume``/``scan_candidates`` in
``queue_drain``'s globals (where the body lives). Tests that monkeypatch those
names on this facade must still affect the drain — ``__setattr__`` mirrors
them into ``queue_drain`` so pre-split patch targets keep working.
"""

from __future__ import annotations

import sys
import types

from cairn.kernel.queue_drain import run_trigger
from cairn.kernel.queue_ledger import claim, consume, scan_candidates, stuck_claims
from cairn.kernel.trigger_host import (
    Trigger,
    TriggerStatus,
    list_installed_triggers,
    load_triggers,
    remove_trigger,
    render_trigger_launchd,
    render_trigger_systemd,
    sync_triggers,
    trigger_launchd_label,
    trigger_systemd_unit_names,
    watch_dir,
)

# Public surface — explicit re-exports (no star imports).
__all__ = [
    "Trigger",
    "TriggerStatus",
    "claim",
    "consume",
    "list_installed_triggers",
    "load_triggers",
    "remove_trigger",
    "render_trigger_launchd",
    "render_trigger_systemd",
    "run_trigger",
    "scan_candidates",
    "stuck_claims",
    "sync_triggers",
    "trigger_launchd_label",
    "trigger_systemd_unit_names",
    "watch_dir",
]


class _TriggerkitFacade(types.ModuleType):
    """Module type that mirrors ledger-name patches into queue_drain's globals."""

    _MIRROR_TO_DRAIN = frozenset({"claim", "consume", "scan_candidates"})

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name in self._MIRROR_TO_DRAIN:
            # Late import: queue_drain is already loaded (we imported run_trigger).
            import cairn.kernel.queue_drain as queue_drain

            setattr(queue_drain, name, value)


sys.modules[__name__].__class__ = _TriggerkitFacade
