"""Work-item contract helpers (W4) — canonical rev derivation for source pullers.

Pullers emit work-item filenames ``p<prio>-<source>-<id>-r<rev>.json`` where
``rev`` is sortable pure digits under the grammar's ``r`` marker. This module
is the single place that derives that rev from a provider ``updated_at``.

See docs/TRIGGERS.md § Source puller contract and docs/FACTORY-PLAN.md §2 T1.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Fixed-width encoding so pure-digit integer comparison is monotonic for every
# pair of revs this helper emits — bare (version default 0) and versioned share
# the same digit count. Variable width was a silent correctness bug: a 16-digit
# 2024 versioned rev outranked a 10-digit 2026 bare rev under rev_is_newer.
#
# Layout under the grammar ``r`` marker:
#   <epoch zero-padded to REV_EPOCH_WIDTH><version zero-padded to REV_VERSION_WIDTH>
# Example (epoch width 11, version width 6): r0001705320000000000
REV_EPOCH_WIDTH = 11  # covers through year ~5138 (10**11 − 1 seconds)
REV_VERSION_WIDTH = 6  # 0 .. 999_999 versions within one second

_REV_EPOCH_MAX = 10**REV_EPOCH_WIDTH  # exclusive upper bound
_REV_VERSION_MAX = 10**REV_VERSION_WIDTH  # exclusive upper bound


def _epoch_seconds(updated_at: str) -> int:
    """Parse an ISO-8601 timestamp to integer UTC seconds since the Unix epoch.

    Accepts ``Z`` or numeric offsets; naive timestamps are treated as UTC.
    Fractional seconds are truncated (not rounded) so two sub-second stamps in
    the same wall second share an epoch and need the version tiebreak.
    """
    if not isinstance(updated_at, str) or not updated_at.strip():
        raise ValueError(f"updated_at must be a non-empty ISO-8601 string, got {updated_at!r}")
    s = updated_at.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"updated_at is not ISO-8601: {updated_at!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def work_item_rev(updated_at: str, version: int | None = None) -> str:
    """Derive a fixed-width sortable work-item rev token ``r<epoch><version>``.

    Parameters
    ----------
    updated_at:
        Provider ``updated_at`` as ISO-8601 (``Z`` or offset). Converted to
        integer UTC epoch seconds. Must be ≥ 1970-01-01 (non-negative epoch);
        pre-epoch timestamps raise :class:`ValueError` (fail loud — the
        grammar cannot encode a leading ``-``).
    version:
        Optional provider version counter for same-second tiebreaks. Defaults
        to ``0`` when omitted so every emit shares one width. Must be in
        ``0 .. 10**REV_VERSION_WIDTH - 1``; overflow raises :class:`ValueError`
        (never silently wraps).

    Returns
    -------
    str
        A token starting with ``r`` suitable for the filename slot
        ``…-<token>.json``. Digits under the marker are always
        ``REV_EPOCH_WIDTH + REV_VERSION_WIDTH`` long, zero-padded:

        ``r{epoch:0{REV_EPOCH_WIDTH}d}{version:0{REV_VERSION_WIDTH}d}``

        Example with defaults: ``r0001705320000000000``.

    Ordering under :func:`~cairn.kernel.queue_ledger.rev_is_newer` (unchanged
    pure-digit integer compare): later ``updated_at`` → strictly newer rev
    regardless of whether either side passed an explicit ``version``; higher
    ``version`` at the same second → strictly newer rev. High-order epoch
    digits dominate the version suffix by construction of the fixed layout.
    """
    epoch = _epoch_seconds(updated_at)
    if epoch < 0:
        raise ValueError(
            f"updated_at is before the Unix epoch (got {updated_at!r} → {epoch}); "
            "work_item_rev requires non-negative UTC seconds so the rev stays pure digits"
        )
    if epoch >= _REV_EPOCH_MAX:
        raise ValueError(
            f"updated_at epoch {epoch} exceeds REV_EPOCH_WIDTH={REV_EPOCH_WIDTH} "
            f"(max {_REV_EPOCH_MAX - 1}); refuse rather than wrap"
        )
    if version is None:
        version = 0
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError(f"version must be a non-negative int, got {version!r}")
    if version < 0 or version >= _REV_VERSION_MAX:
        raise ValueError(
            f"version must be in 0..{_REV_VERSION_MAX - 1} "
            f"(REV_VERSION_WIDTH={REV_VERSION_WIDTH}), got {version}"
        )
    return (
        f"r{epoch:0{REV_EPOCH_WIDTH}d}"
        f"{version:0{REV_VERSION_WIDTH}d}"
    )
