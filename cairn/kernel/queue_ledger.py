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

from cairn.kernel.durafs import atomic_write_text, durable_link, durable_move, durable_unlink
from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.gckit import clear_queue_pin
from cairn.kernel.runstate import load_run
from cairn.kernel.trail import last_trail_terminal
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


def _hardlink(src: Path, dest: Path, *, fs: Any = None) -> None:
    """Hard-link ``src`` into ``dest`` via :func:`durable_link` (D10 single seam).

    Mechanical ops ride durafs (link + dest-parent fsync). Caller-owned POLICY stays
    here: EXDEV reworded as :class:`CairnError`; platform that cannot target a symlink
    without following it refused when ``src`` is a symlink; ``FileNotFoundError`` /
    ``FileExistsError`` pass through for race/collision handling.

    Keyword-only ``fs=`` is the fstestkit injection seam (never monkeypatch ``os.*``).
    """
    src, dest = Path(src), Path(dest)
    # Policy: platforms that cannot hard-link a symlink without following it refuse loudly.
    _link = getattr(os, "link")
    if _link not in os.supports_follow_symlinks and src.is_symlink():
        raise CairnError(
            f"cannot link symlinked event {src} into {dest}: this platform's link "
            "cannot target a symlink without dereferencing it"
        )
    try:
        durable_link(src, dest, fs=fs)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise CairnError(
                f"cannot link {src} into {dest}: source and destination are on "
                "different filesystems (cross-device link)"
            ) from exc
        raise


def claim(watch_abs: Path, candidate: Path, *, fs: Any = None) -> Path | None:
    """Claim ``candidate`` by hard-linking it into ``<watch_abs>/.claim/`` and unlinking
    the original — never ``Path.rename``, because POSIX rename SILENTLY REPLACES an
    existing destination. A stuck claim left behind by an earlier crash is exactly the
    operator evidence :func:`stuck_claims` exists to surface; a rename-based claim would
    let a same-named new candidate destroy it with no error and no trace.

    :func:`durable_link` (via :func:`_hardlink`) fails atomically with ``FileExistsError``
    when the destination name is taken, so a genuine name collision — a different event
    that happens to share a filename with something already sitting in ``.claim/`` — gets
    the same ``-v2`` collision-suffix treatment :func:`_place` uses for ``.done``/
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
            _hardlink(candidate, dest, fs=fs)
        except FileNotFoundError:
            return None
        except FileExistsError:
            if _links_same_source(candidate, dest):
                try:
                    durable_unlink(candidate, fs=fs)
                except FileNotFoundError:
                    pass
                return None
            suffix += 1
            dest_name = f"{stem}-v{suffix}{ext}"
            continue
        break
    # missing_ok: a losing racer that hit the FileExistsError branch above may have
    # already unlinked candidate (it links to the same inode we just landed at dest) —
    # the postcondition (dest valid, candidate gone) holds regardless of which of the
    # two racers physically performs this unlink (G1).
    try:
        durable_unlink(candidate, fs=fs)
    except FileNotFoundError:
        pass
    return dest


def _links_same_source(candidate: Path, dest: Path) -> bool:
    """Whether ``dest`` (which just refused a new hard link under this name) is already
    a hard link to ``candidate`` itself, rather than an unrelated file that merely shares
    a name. ``lstat`` (not ``stat``) compares by the symlink's own inode when
    ``candidate`` is one, matching the non-following link :func:`_hardlink` makes."""
    try:
        c = candidate.lstat()
    except FileNotFoundError:
        return True  # candidate already gone: someone else clearly won it
    try:
        d = dest.lstat()
    except FileNotFoundError:
        return False  # dest vanished between the FileExistsError and here: retry
    return (c.st_dev, c.st_ino) == (d.st_dev, d.st_ino)


def _place(claim_path: Path, dest_dir: Path, name: str, *, fs: Any = None) -> Path:
    """Move ``claim_path`` into ``dest_dir`` under ``name`` (symlink-safe, never overwrite).

    Genuine name collisions (a *different* file already at ``dest``) get a ``-v2``,
    ``-v3``, ... suffix (the ``bootstrap_run`` convention). A concurrent racer that
    already linked the *same* source inode into ``dest`` is a lost race, not a
    collision: return the existing ``dest`` and tolerate a source unlink that the
    winner already performed (same discipline as :func:`claim` / ``_links_same_source``).
    Mechanical ops: :func:`durable_link` + :func:`durable_unlink` via :func:`_hardlink`.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem, ext = Path(name).stem, Path(name).suffix
    dest_name = name
    suffix = 1
    while True:
        dest = dest_dir / dest_name
        try:
            _hardlink(claim_path, dest, fs=fs)
        except FileExistsError:
            if _links_same_source(claim_path, dest):
                # Lost race: winner already placed this inode under dest_name.
                try:
                    durable_unlink(claim_path, fs=fs)
                except FileNotFoundError:
                    pass
                return dest
            suffix += 1
            dest_name = f"{stem}-v{suffix}{ext}"
            continue
        break
    try:
        durable_unlink(claim_path, fs=fs)
    except FileNotFoundError:
        pass  # concurrent winner unlinked first
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
    """Durably write one JSON-line pointer record (D8 outcome class when known).

    ``child_pid`` is historical-by-construction: recorded at spawn time for W3 lease
    prep. By the time a pointer lives in ``.waiting/`` the child has already exited
    (park happens after ``wait()``); this field is not a liveness signal — leases own
    liveness in W3.
    """
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

    Concurrent racers: ``write_pointer`` is idempotent (atomic replace); a source
    unlink that loses the race (winner already unlinked) is benign
    (``FileNotFoundError`` swallowed). Same-content dest already present is fine —
    we rewrite then drop source.
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
        try:
            durable_unlink(src_ptr, fs=fs)
        except FileNotFoundError:
            pass  # concurrent winner already dropped the source pointer


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
        # Waiting parks retain the reciprocal gc pin (judgment still pending).
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
        return _place(claim_path, dest_lane, name, fs=fs)

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
        placed = _place(claim_path, dest_lane, name, fs=fs)
        _tombstone(watch_abs, name, fs=fs)
        # T3 pin-release: terminal ledger placement FIRST, then clear pin.
        if run_dir_s:
            clear_queue_pin(Path(run_dir_s), fs=fs)
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
        try:
            durable_unlink(claim_path, fs=fs)
        except FileNotFoundError:
            pass
        if src_ptr.is_file():
            try:
                durable_unlink(src_ptr, fs=fs)
            except FileNotFoundError:
                pass
        _tombstone(watch_abs, name, fs=fs)
        if run_dir_s:
            clear_queue_pin(Path(run_dir_s), fs=fs)
        return None
    placed = _place(claim_path, watch_abs / ".done", name, fs=fs)
    if src_ptr.is_file():
        try:
            durable_unlink(src_ptr, fs=fs)
        except FileNotFoundError:
            pass
    _tombstone(watch_abs, name, fs=fs)
    # T3 pin-release: terminal ledger placement FIRST, then clear pin.
    if run_dir_s:
        clear_queue_pin(Path(run_dir_s), fs=fs)
    return placed


def unclaim(watch_abs: Path, claim_path: Path, *, fs: Any = None) -> Path:
    """Return a claimed item to the watch-dir root (mint refusal: not an outcome).

    Leaves any claim-side pointer in place only if present — callers should not have
    written one yet when unclaiming on mint refusal. Collision suffix if a same-named
    inbox file already exists.
    """
    watch_abs = Path(watch_abs)
    claim_path = Path(claim_path)
    return _place(claim_path, watch_abs, claim_path.name, fs=fs)


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


def count_by_class(watch_abs: Path, *, glob: str = "*") -> dict[str, int]:
    """Live depth counts from ledger pointer outcome classes (D8) + spool.

    Cheap: one readdir per lane + pointer JSON reads — never opens trails.
    Waiting-class splits (needs_human / blocked / capacity) come from
    ``.waiting/.runs/`` pointer ``exit_code`` via :func:`classify_exit`.
    Items in ``.waiting/`` whose pointer is missing or unclassifiable still
    count toward ``waiting`` / ``inflight`` / ``wip`` but not a specific class.

    Returns keys:
    - ``needs_human``, ``blocked``, ``capacity`` — waiting-class depths
    - ``claimed`` — top-level files in ``.claim/`` (inflight children / stuck)
    - ``waiting`` — all files in ``.waiting/``
    - ``inflight`` — ``claimed + waiting`` (live WIP)
    - ``spool`` — inbox candidates matching ``glob`` (pre-claim)
    - ``failed``, ``done``, ``stuck`` — same as :func:`ledger_counts`
    """
    watch_abs = Path(watch_abs)
    base = ledger_counts(watch_abs)
    claimed = base["stuck"]  # top-level .claim/ files
    waiting_total = base["waiting"]

    needs_human = blocked = capacity = 0
    waiting_lane = watch_abs / ".waiting"
    runs = pointer_dir(waiting_lane)
    if runs.is_dir():
        for ptr in runs.iterdir():
            if not ptr.is_file():
                continue
            # Skip quarantine artifacts from corrupt-pointer repairs.
            if ptr.name.endswith(".corrupt") or ".corrupt-v" in ptr.name:
                continue
            try:
                rec = read_pointer(ptr)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            code = rec.get("exit_code")
            if not isinstance(code, int):
                continue
            outcome = classify_exit(code)
            if outcome.outcome is not OutcomeClass.WAITING or outcome.waiting_kind is None:
                continue
            if outcome.waiting_kind == "needs_human":
                needs_human += 1
            elif outcome.waiting_kind == "blocked":
                blocked += 1
            elif outcome.waiting_kind == "capacity":
                capacity += 1

    spool = len(scan_candidates(watch_abs, glob))
    return {
        "needs_human": needs_human,
        "blocked": blocked,
        "capacity": capacity,
        "claimed": claimed,
        "waiting": waiting_total,
        "inflight": claimed + waiting_total,
        "spool": spool,
        "failed": base["failed"],
        "done": base["done"],
        "stuck": base["stuck"],
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


def _quarantine_corrupt_pointer(
    watch_abs: Path, pointer: Path, name: str, *, reason: str, fs: Any = None
) -> str:
    """Move an unparseable pointer to ``.failed/.runs/<name>.corrupt``; return diagnostic."""
    failed_runs = pointer_dir(Path(watch_abs) / ".failed")
    failed_runs.mkdir(parents=True, exist_ok=True)
    dest = failed_runs / f"{name}.corrupt"
    # Collision on quarantine name: suffix so we never destroy prior evidence.
    if dest.exists():
        n = 2
        while (failed_runs / f"{name}.corrupt-v{n}").exists():
            n += 1
        dest = failed_runs / f"{name}.corrupt-v{n}"
    try:
        durable_move(pointer, dest, fs=fs)
    except FileNotFoundError:
        return f"corrupt pointer {name} vanished before quarantine ({reason})"
    except FileExistsError:
        # Racer quarantined first — drop our copy if still present.
        try:
            durable_unlink(pointer, fs=fs)
        except FileNotFoundError:
            pass
        return f"corrupt pointer {name} already quarantined by concurrent repair ({reason})"
    return f"quarantined corrupt pointer {name} → {dest} ({reason})"


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

    Lost-race discipline: concurrent repairs of the same interrupted move treat
    ``FileExistsError`` / ``FileNotFoundError`` on the completing move as benign when
    the destination already holds the item. Corrupt pointer content is quarantined
    (not silently deleted); only a well-formed orphan with no validated run is removed.
    """
    watch_abs = Path(watch_abs)
    items = _ledger_item_locations(watch_abs, name)
    ptr_lane = pointer.parent.parent  # .../<lane>/.runs/<name> → <lane>
    expected_item = ptr_lane / name

    if expected_item.is_file():
        # Consistent — or a racer already finished the item move; drop a leftover source
        # if it is the same inode still lingering in another lane.
        for extra in items:
            if extra == expected_item:
                continue
            if _links_same_source(extra, expected_item):
                try:
                    durable_unlink(extra, fs=fs)
                except FileNotFoundError:
                    pass
        return None, None

    if items:
        # Pointer moved; item still elsewhere — complete the move into pointer's lane.
        src_item = items[0]
        if src_item != expected_item:
            dest_dir = ptr_lane
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / name
            try:
                if dest.exists():
                    if _links_same_source(src_item, dest):
                        try:
                            durable_unlink(src_item, fs=fs)
                        except FileNotFoundError:
                            pass
                    return dest, None
                durable_move(src_item, dest, fs=fs)
            except FileExistsError:
                # Concurrent repair won the dest slot.
                if dest.exists() and (
                    not src_item.exists() or _links_same_source(src_item, dest)
                ):
                    try:
                        durable_unlink(src_item, fs=fs)
                    except FileNotFoundError:
                        pass
                    return dest, None
                raise
            except FileNotFoundError:
                # Source vanished mid-move — winner finished, or item already gone.
                if dest.exists():
                    return dest, None
                return None, (
                    f"repair of {name}: source item vanished and dest missing — skipped"
                )
            return dest, None
        return None, None

    # Pointer without item anywhere. Corrupt content ≠ orphan: quarantine evidence.
    try:
        rec = read_pointer(pointer)
    except (ValueError, json.JSONDecodeError) as exc:
        diag = _quarantine_corrupt_pointer(
            watch_abs, pointer, name, reason=str(exc), fs=fs
        )
        return None, diag
    except OSError as exc:
        return None, f"pointer {name} unreadable ({exc}) — left in place"

    run_dir = Path(rec["run_dir"]) if rec.get("run_dir") else None
    if run_dir is not None:
        try:
            load_run(run_dir)
        except (OSError, ValueError, ConfigError, json.JSONDecodeError):
            pass  # no validated run — fall through to orphan delete
        else:
            return None, (
                f"pointer {pointer.name} has no item in any ledger lane but a validated "
                f"run.json exists at {run_dir} — left for operator/audit"
            )
    # Well-formed orphan: no item, no validated run — drop pointer.
    try:
        durable_unlink(pointer, fs=fs)
    except FileNotFoundError:
        pass  # concurrent repair already cleaned it
    return None, f"deleted orphan pointer {pointer.name} (no item, no validated run.json)"


def sweep(watch_abs: Path, *, on_done: str, fs: Any = None) -> SweepReport:
    """Advance ``.waiting/`` entries from trail evidence (FACTORY-PLAN T6).

    For each waiting item: read its pointer and the run's trail; route by the last
    terminal event — ``run-done`` → retire DONE; failure-class ``run-halt`` → FAILED;
    waiting-class halt → leave. Vanished run dir → FAILED + diagnostic naming gc.

    Pointer repair (T5): pointer-without-item completes an interrupted move when the
    item is found in another lane; corrupt pointers are quarantined; only a well-formed
    orphan with no validated ``run.json`` is deleted. Item-without-pointer is left as
    stuck evidence (not guessed).

    Per-item isolation: one item's hazard is recorded in ``diagnostics`` and the sweep
    continues (same posture as ``run_trigger``'s per-candidate loop). An uncaught
    exception escaping this function is a bug.
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
            # Skip quarantine artifacts left by prior corrupt-pointer repairs.
            if ptr.name.endswith(".corrupt") or ".corrupt-v" in ptr.name:
                continue
            item_path = watch_abs / lane / ptr.name
            if item_path.is_file():
                continue
            try:
                result, diag = _repair_pointer_item_pair(
                    watch_abs, name=ptr.name, pointer=ptr, on_done=on_done, fs=fs
                )
                if result is not None:
                    repaired.append(result)
                if diag:
                    diagnostics.append(diag)
            except Exception as exc:  # noqa: BLE001 — per-item isolation
                diagnostics.append(f"repair of {ptr.name} hazarded: {exc}")

    # Advance / leave each .waiting/ item (isolated).
    if waiting.is_dir():
        for item in sorted(p for p in waiting.iterdir() if p.is_file()):
            try:
                ptr = pointer_path(waiting, item.name)
                if not ptr.is_file():
                    left.append(item)
                    diagnostics.append(
                        f"waiting item {item.name} has no pointer — left in place "
                        f"(stuck; do not guess)"
                    )
                    continue

                try:
                    rec = read_pointer(ptr)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    left.append(item)
                    diagnostics.append(
                        f"waiting item {item.name}: unreadable pointer ({exc})"
                    )
                    continue

                run_dir_s = rec.get("run_dir") or ""
                run_dir = Path(run_dir_s) if run_dir_s else None
                if run_dir is None or not run_dir.is_dir():
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
                        f"waiting item {item.name}: run dir vanished "
                        f"({run_dir_s or 'unset'}) — retired to .failed/ (possible gc)"
                    )
                    continue

                kind, halt_code = last_trail_terminal(run_dir)
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
                        moved.append(item)
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
                left.append(item)
            except Exception as exc:  # noqa: BLE001 — per-item isolation
                diagnostics.append(f"waiting item {item.name} hazarded: {exc}")
                if item.is_file():
                    left.append(item)

    return SweepReport(
        moved=tuple(moved),
        left=tuple(left),
        repaired=tuple(repaired),
        diagnostics=tuple(diagnostics),
    )
