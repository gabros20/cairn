"""queue_ledger — claim/ledger mechanics for trigger inboxes (pure state).

Bottom layer of the triggerkit split (docs/FACTORY-PLAN.md §3 W0.5 / D10): paths in,
paths out. No Trigger objects, no host backends, no child process orchestration.
Imported by queue_drain and trigger_host; imports neither sibling.

QTP retire-side (W1a / T3–T6): ``retire(outcome)`` routes by :class:`RunOutcome`;
run-dir pointers live under each lane's ``.runs/``; ``sweep`` advances ``.waiting/``
from trail evidence. All moves go through :mod:`cairn.kernel.durafs`.
"""

from __future__ import annotations

import errno
import fnmatch
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairn.kernel.durafs import atomic_write_text, durable_move, durable_unlink, fsync_dir
from cairn.kernel.errors import CairnError
from cairn.kernel.trail import read_trail
from cairn.kernel.types import OutcomeClass, RunOutcome, classify_exit


# Ledger lanes under a watch dir (dot-prefixed; excluded from scan by construction).
_LANES = (".claim", ".waiting", ".failed", ".done")
_POINTER_SUBDIR = ".runs"
_TOMBSTONE_SUBDIR = "tombstones"


# --------------------------------------------------------------------------- #
# Inbox scan
# --------------------------------------------------------------------------- #


def scan_candidates(watch_abs: Path, glob: str) -> list[Path]:
    """Top-level files in ``watch_abs`` matching ``glob``, sorted.

    Never recurses; excludes directories and any name starting with ``.`` (which keeps
    the ``.claim``/``.done``/``.failed``/``.waiting`` ledger dirs below out of the scan by
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
# Claim / retire — the at-most-once ledger (TRIGGERS-PLAN.md §2, FACTORY-PLAN T3)
# --------------------------------------------------------------------------- #


def _hardlink(src: Path, dest: Path) -> None:
    """Hard-link ``src`` into ``dest`` without ever dereferencing a symlinked ``src``.

    ``os.link``'s default ``follow_symlinks=True`` would resolve a symlinked event file
    and link its TARGET, not the symlink — a scan-accepted symlink (``Path.is_file()``
    follows symlinks, so ``scan_candidates`` happily accepts one) would then silently
    ledger the wrong bytes and orphan the real target outside the watch dir entirely.
    Passing ``follow_symlinks=False`` links the symlink itself, so it moves through the
    ledger intact and its target is never read here.

    After a successful link, fsyncs ``dest``'s parent (durafs durable_link order) so the
    new directory entry is durable before callers unlink the source.

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
    fsync_dir(dest.parent)


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
    ``.failed``/``.waiting``, rather than an overwrite: both the stuck claim and the new
    one survive under distinct names.

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
    fsync_dir(candidate.parent)
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
    """Move ``claim_path`` into ``dest_dir`` under ``name`` (symlink-safe, never overwrite).

    Collision gets a ``-v2``, ``-v3``, ... suffix before the extension (the
    ``bootstrap_run`` convention — see ``walk.py``'s ``-v2`` collision handling).
    Uses :func:`_hardlink` + durable unlink (durafs order: dest parent fsynced before
    source disappears). ``os.link`` fails atomically with ``FileExistsError`` when the
    target name is taken, so each attempt is race-safe even though the retry loop around
    it is not.
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
    durable_unlink(claim_path)
    return dest


# --------------------------------------------------------------------------- #
# Pointers — run linkage (FACTORY-PLAN T5)
# --------------------------------------------------------------------------- #


def pointer_dir(lane_dir: Path) -> Path:
    """``<lane>/.runs/`` — where pointer records for items in that lane live."""
    return Path(lane_dir) / _POINTER_SUBDIR


def pointer_path(lane_dir: Path, item_name: str) -> Path:
    """Pointer file for work-item ``item_name`` under ``lane_dir``'s ``.runs/``."""
    return pointer_dir(lane_dir) / item_name


def write_pointer(
    path: Path,
    *,
    run_dir: Path | str,
    outcome: str | None = None,
    exit_code: int | None = None,
    child_pid: int | None = None,
    fs: Any = None,
) -> None:
    """Durably write one JSON-line pointer record (D8 outcome class when known)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "run_dir": str(run_dir),
        "outcome": outcome,
        "exit_code": exit_code,
        "child_pid": child_pid,
    }
    atomic_write_text(path, json.dumps(rec, ensure_ascii=False) + "\n", fs=fs)


def read_pointer(path: Path) -> dict[str, Any]:
    """Parse a pointer file; raises ``ValueError`` on missing/corrupt content."""
    path = Path(path)
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"empty pointer {path}")
    # One JSON line (tolerate trailing blank lines).
    line = text.splitlines()[0]
    doc = json.loads(line)
    if not isinstance(doc, dict) or "run_dir" not in doc:
        raise ValueError(f"pointer {path} missing run_dir")
    return doc


def _tombstone(watch_abs: Path, name: str, *, fs: Any = None) -> Path:
    """Create an empty tombstone marker under ``.done/tombstones/<name>`` (T1/T3)."""
    tomb_dir = Path(watch_abs) / ".done" / _TOMBSTONE_SUBDIR
    tomb_dir.mkdir(parents=True, exist_ok=True)
    dest = tomb_dir / name
    # Empty marker; collision is fine (re-entry after prior retire of same name).
    if not dest.exists():
        atomic_write_text(dest, "", fs=fs)
    return dest


def _move_pointer(src: Path, dest: Path, *, fs: Any = None) -> None:
    """Pointer-first move: durable_move of the pointer file (T5)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        # Unlikely name collision on pointer; -v2 not used for pointers — overwrite is
        # wrong; durable_move raises FileExistsError. Drop dest only if identical content
        # race: prefer leaving src and letting repair complete later.
        durable_unlink(dest, fs=fs)
    durable_move(src, dest, fs=fs)


def _resolve_pointer_fields(
    src_ptr: Path,
    *,
    run_dir: Path | str | None,
    exit_code: int | None,
    child_pid: int | None,
) -> tuple[str, int | None, int | None]:
    """Fill run_dir / exit_code / child_pid from an existing pointer when omitted."""
    existing: dict[str, Any] | None = None
    if src_ptr.is_file():
        try:
            existing = read_pointer(src_ptr)
        except (OSError, ValueError, json.JSONDecodeError):
            existing = None
    if run_dir is None and existing is not None:
        run_dir = existing.get("run_dir")
    if child_pid is None and existing is not None:
        child_pid = existing.get("child_pid")
    if exit_code is None and existing is not None:
        exit_code = existing.get("exit_code")
    return str(run_dir or ""), exit_code, child_pid


def _relocate_pointer(
    src_ptr: Path,
    dest_ptr: Path,
    *,
    run_dir: str,
    outcome: str,
    exit_code: int | None,
    child_pid: int | None,
    fs: Any = None,
) -> None:
    """Pointer-first: write final content at destination, drop source if different.

    Order for crash recovery (T5): dest pointer is durable before the item moves.
    Writing dest first (not move-then-rewrite) means a crash leaves a complete dest
    pointer even when the item is still in the source lane — :func:`sweep` repairs.
    """
    write_pointer(
        dest_ptr,
        run_dir=run_dir,
        outcome=outcome,
        exit_code=exit_code,
        child_pid=child_pid,
        fs=fs,
    )
    if src_ptr.is_file() and src_ptr.resolve() != dest_ptr.resolve():
        durable_unlink(src_ptr, fs=fs)


def retire(
    watch_abs: Path,
    claim_path: Path,
    *,
    outcome: RunOutcome,
    on_done: str,
    exit_code: int | None = None,
    child_pid: int | None = None,
    run_dir: Path | str | None = None,
    fs: Any = None,
) -> Path | None:
    """Retire a claimed (or waiting) item by :class:`RunOutcome` (FACTORY-PLAN T3).

    Routing:
    - ``DONE`` + ``on_done=done`` → ``.done/`` + tombstone; ``on_done=delete`` → unlink
      item + tombstone still (T1: every terminal retire tombstones).
    - ``WAITING`` (any kind) → ``.waiting/`` (reservation retained; no tombstone).
    - ``FAILED`` → ``.failed/`` + tombstone.

    Pointer order (T5): write/move the pointer FIRST, then the item. Pointer content
    records outcome class (D8 depth-count contract), exit_code, child_pid, run_dir.
    Returns the final item path, or ``None`` when the item was deleted (``on_done=delete``).
    """
    watch_abs = Path(watch_abs)
    claim_path = Path(claim_path)
    name = claim_path.name
    src_lane = claim_path.parent  # .claim or .waiting (sweep re-retires from waiting)
    src_ptr = pointer_path(src_lane, name)
    run_dir_s, exit_code, child_pid = _resolve_pointer_fields(
        src_ptr, run_dir=run_dir, exit_code=exit_code, child_pid=child_pid
    )
    outcome_s = outcome.outcome.value

    if outcome.outcome is OutcomeClass.WAITING:
        dest_lane = watch_abs / ".waiting"
        dest_lane.mkdir(parents=True, exist_ok=True)
        _relocate_pointer(
            src_ptr,
            pointer_path(dest_lane, name),
            run_dir=run_dir_s,
            outcome=outcome_s,
            exit_code=exit_code,
            child_pid=child_pid,
            fs=fs,
        )
        return _place(claim_path, dest_lane, name)

    if outcome.outcome is OutcomeClass.FAILED:
        dest_lane = watch_abs / ".failed"
        dest_lane.mkdir(parents=True, exist_ok=True)
        _relocate_pointer(
            src_ptr,
            pointer_path(dest_lane, name),
            run_dir=run_dir_s,
            outcome=outcome_s,
            exit_code=exit_code,
            child_pid=child_pid,
            fs=fs,
        )
        placed = _place(claim_path, dest_lane, name)
        _tombstone(watch_abs, name, fs=fs)
        return placed

    # DONE — terminal: item to .done/ (or delete), drop live pointer, always tombstone.
    assert outcome.outcome is OutcomeClass.DONE
    if src_ptr.is_file():
        # Final pointer content first (crash window leaves evidence), then item, then drop.
        write_pointer(
            src_ptr,
            run_dir=run_dir_s,
            outcome=outcome_s,
            exit_code=exit_code if exit_code is not None else 0,
            child_pid=child_pid,
            fs=fs,
        )
    if on_done == "delete":
        durable_unlink(claim_path, fs=fs)
        if src_ptr.is_file():
            durable_unlink(src_ptr, fs=fs)
        _tombstone(watch_abs, name, fs=fs)
        return None
    placed = _place(claim_path, watch_abs / ".done", name)
    if src_ptr.is_file():
        durable_unlink(src_ptr, fs=fs)
    _tombstone(watch_abs, name, fs=fs)
    return placed



def unclaim(watch_abs: Path, claim_path: Path) -> Path:
    """Return a claimed item to the watch-dir root (mint refusal: not an outcome).

    Leaves any claim-side pointer in place only if present — callers should not have
    written one yet when unclaiming on mint refusal. Collision suffix if a same-named
    inbox file already exists.
    """
    watch_abs = Path(watch_abs)
    claim_path = Path(claim_path)
    return _place(claim_path, watch_abs, claim_path.name)


def stuck_claims(watch_abs: Path) -> list[Path]:
    """Files sitting in ``.claim/`` — a crash mid-run leaves its claim here, never
    auto-retried; the operator re-drops or discards it (surfaced by ``trigger list``).

    Only top-level files count (``.runs/`` pointer dir is ignored).
    """
    claim_dir = Path(watch_abs) / ".claim"
    if not claim_dir.is_dir():
        return []
    return sorted(p for p in claim_dir.iterdir() if p.is_file())


def ledger_counts(watch_abs: Path) -> dict[str, int]:
    """Per-lane item counts for ``trigger list`` (waiting/failed/done + stuck).

    ``waiting`` counts files in ``.waiting/``; ``failed`` / ``done`` likewise.
    ``stuck`` is top-level files in ``.claim/`` (same as :func:`stuck_claims`).
    """
    watch_abs = Path(watch_abs)

    def _count_files(lane: str) -> int:
        d = watch_abs / lane
        if not d.is_dir():
            return 0
        return sum(1 for p in d.iterdir() if p.is_file())

    return {
        "waiting": _count_files(".waiting"),
        "failed": _count_files(".failed"),
        "done": _count_files(".done"),
        "stuck": len(stuck_claims(watch_abs)),
    }


# --------------------------------------------------------------------------- #
# Sweep — advance .waiting/ from trail evidence (FACTORY-PLAN T6)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SweepReport:
    """Outcome of one :func:`sweep` pass over a watch dir's ``.waiting/`` lane."""

    moved: tuple[Path, ...] = ()
    left: tuple[Path, ...] = ()
    repaired: tuple[Path, ...] = ()
    diagnostics: tuple[str, ...] = ()


def _ledger_item_locations(watch_abs: Path, name: str) -> list[Path]:
    """Every path under ledger lanes that is a file named ``name``."""
    found: list[Path] = []
    for lane in _LANES:
        p = Path(watch_abs) / lane / name
        if p.is_file():
            found.append(p)
    return found


def _all_pointers(watch_abs: Path, name: str) -> list[Path]:
    found: list[Path] = []
    for lane in _LANES:
        p = pointer_path(Path(watch_abs) / lane, name)
        if p.is_file():
            found.append(p)
    return found


def _last_trail_terminal(run_dir: Path) -> tuple[str, int | None]:
    """Return ``('done', None)``, ``('halt', exit_code)``, or ``('none', None)``."""
    kind = "none"
    exit_code: int | None = None
    for ev in read_trail(run_dir):
        event = ev.get("event")
        if event == "run-done":
            kind = "done"
            exit_code = None
        elif event == "run-halt":
            kind = "halt"
            data = ev.get("data") or {}
            raw = data.get("exit_code")
            try:
                exit_code = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                exit_code = None
    return kind, exit_code


def _repair_pointer_item_pair(
    watch_abs: Path,
    *,
    name: str,
    pointer: Path,
    on_done: str,
    fs: Any = None,
) -> tuple[Path | None, str | None]:
    """Complete an interrupted pointer-first move (T5).

    Returns ``(repaired_item_path_or_None, diagnostic_or_None)``.
    """
    watch_abs = Path(watch_abs)
    items = _ledger_item_locations(watch_abs, name)
    ptr_lane = pointer.parent.parent  # .../<lane>/.runs/<name> → <lane>
    expected_item = ptr_lane / name

    if expected_item.is_file():
        return None, None  # consistent

    if items:
        # Pointer moved; item still elsewhere — complete the move into pointer's lane.
        src_item = items[0]
        if src_item != expected_item:
            dest_dir = ptr_lane
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / name
            if not dest.exists():
                durable_move(src_item, dest, fs=fs)
            return dest, None
        return None, None

    # Pointer without item anywhere: keep pointer only if run.json validates.
    try:
        rec = read_pointer(pointer)
        run_dir = Path(rec["run_dir"]) if rec.get("run_dir") else None
    except (OSError, ValueError, json.JSONDecodeError, KeyError):
        run_dir = None
    if run_dir is not None and (run_dir / "run.json").is_file():
        return None, (
            f"pointer {pointer.name} has no item in any ledger lane but run.json exists "
            f"at {run_dir} — left for operator/audit"
        )
    # No item, no validated run — drop orphan pointer.
    durable_unlink(pointer, fs=fs)
    return None, f"deleted orphan pointer {pointer.name} (no item, no validated run.json)"


def sweep(watch_abs: Path, *, on_done: str, fs: Any = None) -> SweepReport:
    """Advance ``.waiting/`` entries from trail evidence (FACTORY-PLAN T6).

    For each waiting item: read its pointer and the run's trail; route by the last
    terminal event — ``run-done`` → retire DONE; failure-class ``run-halt`` → FAILED;
    waiting-class halt → leave. Vanished run dir → FAILED + diagnostic naming gc.

    Pointer repair (T5): pointer-without-item completes an interrupted move when the
    item is found in another lane; only deletes a pointer when no item exists anywhere
    and no validated ``run.json`` remains. Item-without-pointer is left as stuck
    evidence (not guessed).
    """
    watch_abs = Path(watch_abs)
    waiting = watch_abs / ".waiting"
    moved: list[Path] = []
    left: list[Path] = []
    repaired: list[Path] = []
    diagnostics: list[str] = []

    # --- pointer repair across all lanes (crash between pointer move and item) ---
    for lane in _LANES:
        runs = pointer_dir(watch_abs / lane)
        if not runs.is_dir():
            continue
        for ptr in sorted(p for p in runs.iterdir() if p.is_file()):
            item_path = (watch_abs / lane / ptr.name)
            if item_path.is_file():
                continue
            result, diag = _repair_pointer_item_pair(
                watch_abs, name=ptr.name, pointer=ptr, on_done=on_done, fs=fs
            )
            if result is not None:
                repaired.append(result)
            if diag:
                diagnostics.append(diag)

    # Item-without-pointer in .waiting → leave as stuck evidence (do not guess).
    if waiting.is_dir():
        for item in sorted(p for p in waiting.iterdir() if p.is_file()):
            ptr = pointer_path(waiting, item.name)
            if not ptr.is_file():
                left.append(item)
                diagnostics.append(
                    f"waiting item {item.name} has no pointer — left in place (stuck; do not guess)"
                )
                continue

            try:
                rec = read_pointer(ptr)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                left.append(item)
                diagnostics.append(f"waiting item {item.name}: unreadable pointer ({exc})")
                continue

            run_dir_s = rec.get("run_dir") or ""
            run_dir = Path(run_dir_s) if run_dir_s else None
            if run_dir is None or not run_dir.is_dir():
                # Vanished run dir → failed + diagnostic naming gc.
                dest = retire(
                    watch_abs,
                    item,
                    outcome=RunOutcome(outcome=OutcomeClass.FAILED),
                    on_done=on_done,
                    exit_code=rec.get("exit_code"),
                    child_pid=rec.get("child_pid"),
                    run_dir=run_dir_s,
                    fs=fs,
                )
                if dest is not None:
                    moved.append(dest)
                diagnostics.append(
                    f"waiting item {item.name}: run dir vanished ({run_dir_s or 'unset'}) "
                    f"— retired to .failed/ (possible gc)"
                )
                continue

            kind, halt_code = _last_trail_terminal(run_dir)
            if kind == "done":
                dest = retire(
                    watch_abs,
                    item,
                    outcome=RunOutcome(outcome=OutcomeClass.DONE),
                    on_done=on_done,
                    exit_code=0,
                    child_pid=rec.get("child_pid"),
                    run_dir=run_dir,
                    fs=fs,
                )
                if dest is not None:
                    moved.append(dest)
                elif on_done == "delete":
                    moved.append(item)  # logical move (deleted)
                continue
            if kind == "halt" and halt_code is not None:
                outcome = classify_exit(halt_code)
                if outcome.outcome is OutcomeClass.WAITING:
                    left.append(item)
                    continue
                dest = retire(
                    watch_abs,
                    item,
                    outcome=outcome,
                    on_done=on_done,
                    exit_code=halt_code,
                    child_pid=rec.get("child_pid"),
                    run_dir=run_dir,
                    fs=fs,
                )
                if dest is not None:
                    moved.append(dest)
                elif outcome.outcome is OutcomeClass.DONE and on_done == "delete":
                    moved.append(item)
                continue
            # No terminal trail event yet — leave.
            left.append(item)

    return SweepReport(
        moved=tuple(moved),
        left=tuple(left),
        repaired=tuple(repaired),
        diagnostics=tuple(diagnostics),
    )
