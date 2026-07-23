"""Work-item contract helpers (W4) — canonical rev derivation for source pullers.

Pullers emit work-item filenames ``p<prio>-<source>-<id>-r<rev>.json`` where
``rev`` is sortable pure digits under the grammar's ``r`` marker. This module
is the single place that derives that rev from a provider ``updated_at``.

See docs/TRIGGERS.md § Source puller contract and docs/FACTORY-PLAN.md §2 T1.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Zero-pad width for the optional provider version tiebreak. Sources that use
# ``version=`` MUST pass it on every emit (including 0) so pure-digit integer
# order stays monotonic; mixing bare-epoch revs with versioned revs for the
# same source is undefined.
REV_VERSION_WIDTH = 6

# Max exclusive version under :data:`REV_VERSION_WIDTH` (10**W).
_REV_VERSION_MAX = 10**REV_VERSION_WIDTH


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
    """Derive a sortable work-item rev token ``r<epoch>[version]``.

    Parameters
    ----------
    updated_at:
        Provider ``updated_at`` as ISO-8601 (``Z`` or offset). Converted to
        integer UTC epoch seconds.
    version:
        Optional provider version counter for same-second tiebreaks. When
        set, zero-padded to :data:`REV_VERSION_WIDTH` digits and appended so
        the digits under the ``r`` marker remain pure-digit and compare
        correctly under :func:`cairn.kernel.queue_ledger.rev_is_newer`.

    Returns
    -------
    str
        A token starting with ``r`` suitable for the filename slot
        ``…-<token>.json`` (i.e. the grammar's ``r`` marker plus the captured
        rev digits). Example: ``r1705320000`` or ``r1705320000000001``.

    The captured rev digits (everything after the leading ``r``) are pure
    digits so :func:`~cairn.kernel.queue_ledger.rev_order` /
    :func:`~cairn.kernel.queue_ledger.rev_is_newer` order them numerically:
    later ``updated_at`` → strictly newer rev; higher ``version`` at the same
    second → strictly newer rev.
    """
    epoch = _epoch_seconds(updated_at)
    if version is None:
        return f"r{epoch}"
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError(f"version must be a non-negative int, got {version!r}")
    if version < 0 or version >= _REV_VERSION_MAX:
        raise ValueError(
            f"version must be in 0..{_REV_VERSION_MAX - 1} "
            f"(REV_VERSION_WIDTH={REV_VERSION_WIDTH}), got {version}"
        )
    return f"r{epoch}{version:0{REV_VERSION_WIDTH}d}"
