"""triggerkit — compatibility facade over the W0.5 three-way split.

The implementation lives in three modules (docs/FACTORY-PLAN.md §3 W0.5 / D10):

- :mod:`cairn.kernel.queue_ledger` — claim/ledger mechanics (pure state)
- :mod:`cairn.kernel.queue_drain` — drain loop (process orchestration)
- :mod:`cairn.kernel.trigger_host` — declaration + host-watcher integration

This module re-exports the public surface so every existing importer
(``cli.py``, tests) keeps working unchanged. New code may import from the
home modules directly.

Ledger names are **not** mirrored into ``queue_drain``: tests that need to
patch claim/retire/sweep target ``cairn.kernel.queue_drain`` (or pass through
the drain's module globals) — the setattr facade is gone (W1a / T5 review).
"""

from __future__ import annotations

from cairn.kernel.queue_drain import (
    ReconcileReport,
    TriggerReconcileSummary,
    reconcile_workspace,
    run_trigger,
)
from cairn.kernel.queue_ledger import (
    DEFAULT_LEASE_TTL_S,
    LEDGER_VERSION,
    LEASE_TTL_DEFAULT,
    LEASE_TTL_OFF,
    SweepReport,
    audit_and_quarantine,
    audit_ledger,
    boot_id,
    check_ledger_version,
    claim,
    count_by_class,
    effective_lease_ttl,
    ledger_counts,
    mop_stranded_deferred,
    pid_alive,
    read_ledger_version,
    retire,
    scan_candidates,
    stamp_ledger_version,
    stuck_claims,
    sweep,
    write_lease,
)
from cairn.kernel.trigger_host import (
    RECONCILE_BEAT_NAME,
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
from cairn.kernel.wsid import label_prefix_for, workspace_id, ws8

# Public surface — explicit re-exports (no star imports).
__all__ = [
    "DEFAULT_LEASE_TTL_S",
    "LEDGER_VERSION",
    "LEASE_TTL_DEFAULT",
    "LEASE_TTL_OFF",
    "RECONCILE_BEAT_NAME",
    "ReconcileReport",
    "SweepReport",
    "Trigger",
    "TriggerReconcileSummary",
    "TriggerStatus",
    "audit_and_quarantine",
    "audit_ledger",
    "boot_id",
    "check_ledger_version",
    "claim",
    "count_by_class",
    "effective_lease_ttl",
    "label_prefix_for",
    "ledger_counts",
    "list_installed_triggers",
    "load_triggers",
    "mop_stranded_deferred",
    "pid_alive",
    "read_ledger_version",
    "reconcile_workspace",
    "remove_trigger",
    "render_trigger_launchd",
    "render_trigger_systemd",
    "retire",
    "run_trigger",
    "scan_candidates",
    "stamp_ledger_version",
    "stuck_claims",
    "sweep",
    "sync_triggers",
    "trigger_launchd_label",
    "trigger_systemd_unit_names",
    "watch_dir",
    "workspace_id",
    "write_lease",
    "ws8",
]
