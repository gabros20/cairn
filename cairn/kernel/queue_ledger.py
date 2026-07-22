"""queue_ledger — claim/ledger mechanics for trigger inboxes (pure state).

Bottom layer of the triggerkit split (docs/FACTORY-PLAN.md §3 W0.5 / D10): paths in,
paths out. No Trigger objects, no host backends, no child process orchestration.
Imported by queue_drain and trigger_host; imports neither sibling.
"""

from __future__ import annotations

import errno
import fnmatch
import os
from pathlib import Path

from cairn.kernel.errors import CairnError


# --------------------------------------------------------------------------- #
# Inbox scan
# --------------------------------------------------------------------------- #


def scan_candidates(watch_abs: Path, glob: str) -> list[Path]:
    """Top-level files in ``watch_abs`` matching ``glob``, sorted.

    Never recurses; excludes directories and any name starting with ``.`` (which keeps
    the ``.claim``/``.done``/``.failed`` ledger dirs below out of the scan by
    construction). A watch dir that doesn't exist yet (no event has landed) scans empty.
    """
    watch_abs = Path(watch_abs)
    if not watch_abs.is_dir():
        return []
    return sorted(
        p
        for p in watch_abs.iterdir()
        if p.is_file() and not p.name.startswith(".") and fnmatch.fnmatch(p.name, glob)
    )



# --------------------------------------------------------------------------- #
# Claim / consume — the at-most-once ledger (TRIGGERS-PLAN.md §2)
# --------------------------------------------------------------------------- #


def _hardlink(src: Path, dest: Path) -> None:
    """Hard-link ``src`` into ``dest`` without ever dereferencing a symlinked ``src``.

    ``os.link``'s default ``follow_symlinks=True`` would resolve a symlinked event file
    and link its TARGET, not the symlink — a scan-accepted symlink (``Path.is_file()``
    follows symlinks, so ``scan_candidates`` happily accepts one) would then silently
    ledger the wrong bytes and orphan the real target outside the watch dir entirely.
    Passing ``follow_symlinks=False`` links the symlink itself, so it moves through the
    ledger intact and its target is never read here.

    ``FileNotFoundError`` (src vanished) and ``FileExistsError`` (dest already taken)
    pass through unchanged for the caller's own race/collision handling. Anything this
    layer can't handle safely becomes a :class:`CairnError` — a clear error beats a
    wrong or corrupted file:

    - a platform whose ``os.link`` can't target a symlink without dereferencing it
      (checked via ``os.supports_follow_symlinks``), when ``src`` actually is one;
    - ``src``/``dest`` on different filesystems (``EXDEV``) — a hard link can never
      cross a filesystem boundary, symlink or not.
    """
    if os.link in os.supports_follow_symlinks:
        link_kwargs: dict[str, bool] = {"follow_symlinks": False}
    elif src.is_symlink():
        raise CairnError(
            f"cannot link symlinked event {src} into {dest}: this platform's os.link "
            "cannot target a symlink without dereferencing it"
        )
    else:
        link_kwargs = {}
    try:
        os.link(src, dest, **link_kwargs)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise CairnError(
                f"cannot link {src} into {dest}: source and destination are on "
                "different filesystems (cross-device link)"
            ) from exc
        raise


def claim(watch_abs: Path, candidate: Path) -> Path | None:
    """Claim ``candidate`` by hard-linking it into ``<watch_abs>/.claim/`` and unlinking
    the original — never ``Path.rename``, because POSIX rename SILENTLY REPLACES an
    existing destination. A stuck claim left behind by an earlier crash is exactly the
    operator evidence :func:`stuck_claims` exists to surface; a rename-based claim would
    let a same-named new candidate destroy it with no error and no trace.

    ``os.link`` (via :func:`_hardlink`) fails atomically with ``FileExistsError`` when
    the destination name is taken, so a genuine name collision — a different event that
    happens to share a filename with something already sitting in ``.claim/`` — gets the
    same ``-v2`` collision-suffix treatment :func:`_place` uses for ``.done``/
    ``.failed``, rather than an overwrite: both the stuck claim and the new one survive
    under distinct names.

    Losing the race to claim ``candidate`` itself (a concurrent claimer already won and
    moved it) surfaces as ``FileNotFoundError`` — caught here and turned into ``None``,
    never raised, exactly as the ``Path.rename`` version did. A ``FileExistsError`` whose
    destination turns out to already be a hard link to ``candidate`` itself (a concurrent
    claimer's link narrowly beat ours to the very same source/name) is also a lost race,
    not a name collision — detected via ``_links_same_source`` before falling through to
    the ``-v2`` suffix path.
    """
    watch_abs = Path(watch_abs)
    candidate = Path(candidate)
    claim_dir = watch_abs / ".claim"
    claim_dir.mkdir(parents=True, exist_ok=True)
    name = candidate.name
    stem, ext = Path(name).stem, Path(name).suffix
    dest_name = name
    suffix = 1
    while True:
        dest = claim_dir / dest_name
        try:
            _hardlink(candidate, dest)
        except FileNotFoundError:
            return None
        except FileExistsError:
            if _links_same_source(candidate, dest):
                candidate.unlink(missing_ok=True)
                return None
            suffix += 1
            dest_name = f"{stem}-v{suffix}{ext}"
            continue
        break
    # missing_ok: a losing racer that hit the FileExistsError branch above may have
    # already unlinked candidate (it links to the same inode we just landed at dest) —
    # the postcondition (dest valid, candidate gone) holds regardless of which of the
    # two racers physically performs this unlink (G1).
    candidate.unlink(missing_ok=True)
    return dest


def _links_same_source(candidate: Path, dest: Path) -> bool:
    """Whether ``dest`` (which just refused a new hard link under this name) is already
    a hard link to ``candidate`` itself, rather than an unrelated file that merely shares
    a name. ``lstat`` (not ``stat``) compares by the symlink's own inode when
    ``candidate`` is one, matching the ``follow_symlinks=False`` link :func:`_hardlink`
    makes."""
    try:
        c = candidate.lstat()
    except FileNotFoundError:
        return True  # candidate already gone: someone else clearly won it
    try:
        d = dest.lstat()
    except FileNotFoundError:
        return False  # dest vanished between the FileExistsError and here: retry
    return (c.st_dev, c.st_ino) == (d.st_dev, d.st_ino)


def _place(claim_path: Path, dest_dir: Path, name: str) -> Path:
    """Hard-link ``claim_path`` into ``dest_dir`` under ``name`` (never dereferencing a
    symlinked claim — see :func:`_hardlink`), never overwriting an existing file: a name
    collision gets a ``-v2``, ``-v3``, ... suffix before the extension (the
    ``bootstrap_run`` convention — see ``walk.py``'s ``-v2`` collision handling).
    ``os.link`` fails atomically with ``FileExistsError`` when the target name is taken,
    so each attempt is race-safe even though the retry loop around it is not.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem, ext = Path(name).stem, Path(name).suffix
    dest_name = name
    suffix = 1
    while True:
        dest = dest_dir / dest_name
        try:
            _hardlink(claim_path, dest)
        except FileExistsError:
            suffix += 1
            dest_name = f"{stem}-v{suffix}{ext}"
            continue
        break
    claim_path.unlink()
    return dest


def consume(watch_abs: Path, claim_path: Path, *, ok: bool, on_done: str) -> Path | None:
    """Retire a claimed file: ``.done/`` (or delete, per ``on_done``) on success,
    ``.failed/`` on failure — a failed event is always moved aside, never left to
    retry-loop as a poison file."""
    watch_abs = Path(watch_abs)
    claim_path = Path(claim_path)
    if not ok:
        return _place(claim_path, watch_abs / ".failed", claim_path.name)
    if on_done == "delete":
        claim_path.unlink()
        return None
    return _place(claim_path, watch_abs / ".done", claim_path.name)


def stuck_claims(watch_abs: Path) -> list[Path]:
    """Files sitting in ``.claim/`` — a crash mid-run leaves its claim here, never
    auto-retried; the operator re-drops or discards it (surfaced by ``trigger list``)."""
    claim_dir = Path(watch_abs) / ".claim"
    if not claim_dir.is_dir():
        return []
    return sorted(p for p in claim_dir.iterdir() if p.is_file())
