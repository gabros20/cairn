"""Work-item contract helpers (W4) — rev derivation + identity-safe filenames.

Pullers emit work-item filenames ``p<prio>-<source>-<id>-r<rev>.json`` where
``rev`` is sortable pure digits under the grammar's ``r`` marker. This module
is the single place that derives that rev from a provider ``updated_at`` and
that sanitizes untrusted upstream ids into the kernel identity grammar
(``safe_item_id`` / ``work_item_filename``) so a hostile id cannot path-traverse
out of the inbox or wedge a puller.

See docs/TRIGGERS.md § Source puller contract and docs/FACTORY-PLAN.md §2 T1.
"""

from __future__ import annotations

import re
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

# Identity-strict grammar (queue_ledger._ITEM_NAME_RE) — id segment only:
#   [a-z0-9]([a-z0-9._-]*[a-z0-9])?
# source segment: [a-z][a-z0-9]*
_ID_DISALLOWED = re.compile(r"[^a-z0-9._-]+")
_ID_SEP_RUN = re.compile(r"[._-]{2,}")
_ID_GRAMMAR = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
_SOURCE_DISALLOWED = re.compile(r"[^a-z0-9]+")
_SOURCE_GRAMMAR = re.compile(r"^[a-z][a-z0-9]*$")


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


# --------------------------------------------------------------------------- #
# Identity-safe id + filename (untrusted upstream → grammar token)
# --------------------------------------------------------------------------- #


def safe_item_id(raw: object) -> str:
    """Sanitize an untrusted upstream id into the identity-strict id grammar.

    Kernel grammar (queue_ledger / FACTORY-PLAN T1)::

        [a-z0-9]([a-z0-9._-]*[a-z0-9])?

    Steps: stringify → lowercase → replace disallowed chars with ``-`` →
    collapse separator runs → strip leading/trailing separators. Path separators
    (``/``, ``\\``) and ``..`` sequences are destroyed by construction — the
    result is always a single path segment with no ``/``.

    Raises :class:`ValueError` when nothing salvageable remains (empty, pure
    punctuation/unicode with no alnum). Fail-loud rather than emit a nonconforming
    or empty id that would break admission or wedge a puller.
    """
    if raw is None:
        raise ValueError("item id is empty/unsalvageable after sanitization: None")
    s = str(raw).strip().lower()
    if not s:
        raise ValueError(f"item id is empty/unsalvageable after sanitization: {raw!r}")
    # Drop path separators and every char outside the grammar charset.
    s = _ID_DISALLOWED.sub("-", s)
    s = _ID_SEP_RUN.sub("-", s)
    s = s.strip("._-")
    if not s or not _ID_GRAMMAR.fullmatch(s):
        raise ValueError(f"item id is empty/unsalvageable after sanitization: {raw!r}")
    return s


def safe_source(raw: object) -> str:
    """Sanitize a source name into ``[a-z][a-z0-9]*`` (identity-strict source)."""
    if raw is None:
        raise ValueError("source is empty/unsalvageable after sanitization: None")
    s = str(raw).strip().lower()
    s = _SOURCE_DISALLOWED.sub("", s)
    if not s or not _SOURCE_GRAMMAR.fullmatch(s):
        raise ValueError(f"source is empty/unsalvageable after sanitization: {raw!r}")
    return s


def clamp_prio(prio: object) -> int:
    """Clamp a priority to a single digit 0–9 (identity-strict prio slot)."""
    try:
        p = int(prio)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"prio must be an int 0-9, got {prio!r}") from exc
    if isinstance(prio, bool):
        raise ValueError(f"prio must be an int 0-9, got {prio!r}")
    return max(0, min(9, p))


def work_item_filename(
    prio: object,
    source: object,
    item_id: object,
    rev: object,
) -> str:
    """Build an identity-strict inbox filename ``p<prio>-<source>-<id>-r<rev>.json``.

    - ``prio`` is clamped to 0–9 (a seam-supplied 42 becomes 9, never ``p42-…``).
    - ``source`` and ``item_id`` go through :func:`safe_source` / :func:`safe_item_id`
      so untrusted upstream strings cannot path-traverse or break the grammar.
    - ``rev`` may be a bare digit string or a full ``r…`` token; the ``r`` marker
      is ensured exactly once.

    The returned basename is always a single path segment (no ``/``) and matches
    :func:`~cairn.kernel.queue_ledger.parse_item_name`.
    """
    p = clamp_prio(prio)
    src = safe_source(source)
    iid = safe_item_id(item_id)
    if rev is None:
        raise ValueError("rev is required")
    rev_s = str(rev).strip()
    if not rev_s:
        raise ValueError("rev is required")
    if not rev_s.startswith("r"):
        rev_s = "r" + rev_s
    # Digits under the marker must be non-empty grammar-safe.
    rev_body = rev_s[1:]
    if not rev_body or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", rev_body):
        raise ValueError(f"rev is not identity-strict: {rev!r}")
    return f"p{p}-{src}-{iid}-{rev_s}.json"
