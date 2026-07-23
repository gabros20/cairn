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
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cairn.kernel.durafs import (
    atomic_write_text,
    durable_link,
    durable_move,
    durable_unlink,
    exclusive_create,
)
from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.gckit import clear_queue_pin
from cairn.kernel.runstate import load_run
from cairn.kernel.trail import last_trail_terminal
from cairn.kernel.types import Finding, OutcomeClass, RunOutcome, classify_exit


# Ledger lanes under a watch dir (dot-prefixed; excluded from scan by construction).
_LANES = (".claim", ".waiting", ".failed", ".done")
_LIVE_LANES = (".claim", ".waiting")  # identity may occupy only one of these
_TERMINAL_LANES = (".failed", ".done")
_POINTER_SUBDIR = ".runs"
_TOMBSTONE_SUBDIR = "tombstones"
_IDS_SUBDIR = ".ids"
_DEFERRED_SUBDIR = ".deferred"
_REJECTED_SUBDIR = ".rejected"
_LEASES_SUBDIR = ".leases"

# Upgrade-safety marker (FACTORY-PLAN T8). A drain/reconcile REFUSES a watch dir
# whose ledger-version is NEWER than this binary understands.
LEDGER_VERSION = 1
LEDGER_VERSION_NAME = "ledger-version"

# POSIX NAME_MAX floor; identity + every ledger-derived name must fit (FACTORY-PLAN T1).
NAME_MAX = 255

# Default admission byte cap for identity:strict triggers (overridable per trigger).
DEFAULT_MAX_ITEM_BYTES = 1_048_576  # 1 MiB

# Orphan-reservation grace (FACTORY-PLAN T1): protect the sub-second reserve→claim
# gap from a concurrent drain's orphan sweeper. Over-holding is safe; only release
# a reservation with no live item AND mtime older than this window.
RESERVATION_GRACE_S = 60.0

# Claim leases (FACTORY-PLAN W3 / T13): default ttl when leases auto-enable
# (concurrency > 1). Serial default-concurrency triggers stay stuck-forever (D7)
# unless ``lease:`` is set explicitly.
DEFAULT_LEASE_TTL_S = 3600  # 60m

# Sentinel for Trigger.lease_ttl_s when the key is absent (default policy).
LEASE_TTL_DEFAULT = -1
# Explicit ``lease: off``.
LEASE_TTL_OFF = 0

# Process-cached boot identity. ``"unknown"`` disables pid-reuse detection
# (different-boot reap still works only when a real id is available).
_BOOT_ID_CACHE: str | None = None
BOOT_ID_UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# Ledger version marker (FACTORY-PLAN T8) — upgrade safety
# --------------------------------------------------------------------------- #


def ledger_version_path(watch_abs: Path) -> Path:
    """``<watch>/ledger-version`` — small int, current = :data:`LEDGER_VERSION`."""
    return Path(watch_abs) / LEDGER_VERSION_NAME


def read_ledger_version(watch_abs: Path) -> int:
    """Return the on-disk ledger-version, or ``0`` when absent (legacy)."""
    path = ledger_version_path(watch_abs)
    if not path.is_file():
        return 0
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    if not text:
        return 0
    try:
        return int(text.split()[0])
    except ValueError:
        # Unparseable marker — treat as legacy (0) so we don't refuse a hand-edited
        # file forever; stamp_ledger_version rewrites a clean LEDGER_VERSION on next write.
        return 0


def check_ledger_version(watch_abs: Path) -> None:
    """Refuse (loud ConfigError, no mutation) when marker is newer than we understand.

    Older-or-equal and absent (legacy = 0) proceed. An older binary must never
    drain a newer ledger (mixed-version upgrade corruption hole, T8).
    """
    on_disk = read_ledger_version(watch_abs)
    if on_disk > LEDGER_VERSION:
        message = (
            f"ledger-version {on_disk} in {watch_abs} is newer than this cairn "
            f"understands (LEDGER_VERSION={LEDGER_VERSION}). Upgrade cairn, or do not "
            "point an older binary at a newer ledger (FACTORY-PLAN T8)."
        )
        raise ConfigError(message, findings=[Finding("error", message)], file=str(ledger_version_path(watch_abs)))


def stamp_ledger_version(watch_abs: Path, *, fs: Any = None) -> None:
    """Write/bump ``ledger-version`` to :data:`LEDGER_VERSION` when older or absent.

    No-op when already equal. Does NOT write when on-disk is newer (caller must
    have refused via :func:`check_ledger_version` first). Creates the watch dir
    if needed.
    """
    watch_abs = Path(watch_abs)
    on_disk = read_ledger_version(watch_abs)
    if on_disk > LEDGER_VERSION:
        return
    if on_disk == LEDGER_VERSION and ledger_version_path(watch_abs).is_file():
        return
    watch_abs.mkdir(parents=True, exist_ok=True)
    atomic_write_text(ledger_version_path(watch_abs), f"{LEDGER_VERSION}\n", fs=fs)


# --------------------------------------------------------------------------- #
# Invariant audit (FACTORY-PLAN T8 / T13 I2)
# --------------------------------------------------------------------------- #


def _claim_has_live_owner(
    watch_abs: Path,
    item_name: str,
    *,
    current_boot_id: str | None = None,
    kill: Callable[[int, int], None] | None = None,
) -> bool:
    """True when ``.claim/<item_name>`` has a lease whose holder is still LIVE.

    Covers the claim→lease→spawn→write_pointer window (T13 C1 / T14 I1): a live
    drain_pid (or live child_pid) means the missing pointer is an in-flight
    transient, not an invariant violation. Absent/unreadable lease → False
    (treat as orphaned for audit purposes).
    """
    path = lease_path(watch_abs, item_name)
    if not path.is_file():
        return False
    try:
        lease = read_lease(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    boot = current_boot_id if current_boot_id is not None else boot_id()
    return not child_is_dead(lease, current_boot_id=boot, kill=kill)


def audit_ledger(
    watch_abs: Path,
    *,
    current_boot_id: str | None = None,
    kill: Callable[[int, int], None] | None = None,
) -> list[str]:
    """Return human-readable invariant violations for ``watch_abs`` (empty = clean).

    Checks (FACTORY-PLAN T8):
    1. every ``.runs`` pointer has a sibling item file and vice versa (per lane)
    2. every grammar-conforming ``.claim`` item has a reservation under ``.claim/.ids/``
       (strict-identity shape — identity:off arbitrary names are skipped)
    3. no identity occupies two live states (``.claim`` and ``.waiting``)
    4. terminal-ledger runs (``.done`` / ``.failed`` pointers) are not queue-pinned

    **Lease liveness (T14 I1):** a ``.claim/`` item without a pointer (or without a
    reservation) whose lease shows a LIVE owner (drain_pid / child_pid alive, same
    boot — :func:`child_is_dead`) is an in-flight drain window, NOT a violation.
    Only a claim with no pointer AND a dead/absent owner is reported.

    Over-preserve: returns issues only — never mutates. Callers (doctor, reconcile)
    surface + may quarantine affected identities; they do NOT auto-delete.
    """
    from cairn.kernel.gckit import QUEUE_PIN_NAME

    watch_abs = Path(watch_abs)
    issues: list[str] = []
    if not watch_abs.is_dir():
        return issues

    boot = current_boot_id if current_boot_id is not None else boot_id()

    # (1) pointer <-> item for each lane
    for lane in _LANES:
        lane_dir = watch_abs / lane
        if not lane_dir.is_dir():
            continue
        items = {
            p.name
            for p in lane_dir.iterdir()
            if p.is_file() and not p.name.startswith(".")
        }
        runs = pointer_dir(lane_dir)
        pointers: set[str] = set()
        if runs.is_dir():
            for p in runs.iterdir():
                if p.is_file() and not p.name.startswith("."):
                    pointers.add(p.name)
        for name in sorted(pointers - items):
            issues.append(f"pointer without item: {lane}/{name}")
        # DONE drops the live pointer after terminal placement (retire); item-without-
        # pointer is the normal .done shape. Live lanes + .failed keep both.
        if lane != ".done":
            for name in sorted(items - pointers):
                # In-flight claim (live lease owner, no pointer yet) is not a violation.
                if lane == ".claim" and _claim_has_live_owner(
                    watch_abs, name, current_boot_id=boot, kill=kill
                ):
                    continue
                issues.append(f"item without pointer: {lane}/{name}")

    # (2) every grammar-conforming .claim item has a reservation
    claim_dir = watch_abs / ".claim"
    if claim_dir.is_dir():
        for p in sorted(claim_dir.iterdir()):
            if not p.is_file() or p.name.startswith("."):
                continue
            item = parse_item_name(p.name)
            if item is None:
                continue  # identity:off / nonconforming — no reservation expected
            if reservation_path(watch_abs, item.identity).is_file():
                continue
            # Live in-flight claim may still be mid-admit; only flag when owner is dead.
            if _claim_has_live_owner(watch_abs, p.name, current_boot_id=boot, kill=kill):
                continue
            issues.append(
                f"claim without reservation: {p.name} (identity {item.identity})"
            )

    # (3) no identity in two live states
    by_identity: dict[str, list[str]] = {}
    for lane in _LIVE_LANES:
        lane_dir = watch_abs / lane
        if not lane_dir.is_dir():
            continue
        for p in lane_dir.iterdir():
            if not p.is_file() or p.name.startswith("."):
                continue
            item = parse_item_name(p.name)
            if item is None:
                continue
            by_identity.setdefault(item.identity, []).append(f"{lane}/{p.name}")
    for identity, locs in sorted(by_identity.items()):
        lanes_hit = {loc.split("/", 1)[0] for loc in locs}
        if len(lanes_hit) > 1:
            issues.append(
                f"identity in two live states: {identity} at {', '.join(sorted(locs))}"
            )

    # (4) terminal-ledger runs are not pinned
    for lane in _TERMINAL_LANES:
        lane_dir = watch_abs / lane
        runs = pointer_dir(lane_dir)
        if not runs.is_dir():
            continue
        for ptr in runs.iterdir():
            if not ptr.is_file() or ptr.name.startswith("."):
                continue
            try:
                rec = read_pointer(ptr)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            run_dir_s = rec.get("run_dir")
            if not run_dir_s:
                continue
            pin = Path(run_dir_s) / QUEUE_PIN_NAME
            if pin.is_file():
                issues.append(
                    f"terminal-ledger run still pinned: {lane}/{ptr.name} -> {run_dir_s}"
                )

    return issues


# --------------------------------------------------------------------------- #
# Liveness helpers (FACTORY-PLAN W3 / T13)
# --------------------------------------------------------------------------- #


def boot_id(*, runner: Any = None, _reset: bool = False) -> str:
    """Stable host boot identity for lease liveness (pid-reuse detection).

    Resolution order:
    1. Linux: ``/proc/sys/kernel/random/boot_id``
    2. macOS: ``sysctl -n kern.boottime`` (via injected ``runner.run`` or
       subprocess — never reads credential files)
    3. Fallback sentinel :data:`BOOT_ID_UNKNOWN` — pid-reuse detection is
       disabled (same-boot ``pid_alive`` still works; a different-boot
       comparison against ``"unknown"`` never forces a reap on its own)

    Cached per process. Pass ``_reset=True`` only from tests.
    """
    global _BOOT_ID_CACHE
    if _reset:
        _BOOT_ID_CACHE = None
    if _BOOT_ID_CACHE is not None:
        return _BOOT_ID_CACHE

    # Linux boot id (kernel random uuid, stable for the boot).
    linux_path = Path("/proc/sys/kernel/random/boot_id")
    try:
        if linux_path.is_file():
            text = linux_path.read_text(encoding="utf-8").strip()
            if text:
                _BOOT_ID_CACHE = text
                return _BOOT_ID_CACHE
    except OSError:
        pass

    # macOS / BSD: kern.boottime string is unique per boot. Tried whenever Linux
    # path is absent (Linux already returned above when the file was readable).
    try:
        if runner is not None:
            result = runner.run(["sysctl", "-n", "kern.boottime"])
            text = (result.stdout or "").strip()
            if getattr(result, "returncode", 0) != 0 or not text:
                text = ""
        else:
            completed = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                capture_output=True,
                text=True,
                check=False,
            )
            text = (completed.stdout or "").strip() if completed.returncode == 0 else ""
        if text:
            _BOOT_ID_CACHE = text
            return _BOOT_ID_CACHE
    except (OSError, AttributeError):
        pass

    _BOOT_ID_CACHE = BOOT_ID_UNKNOWN
    return _BOOT_ID_CACHE


def pid_alive(
    pid: int | None,
    *,
    kill: Callable[[int, int], None] | None = None,
) -> bool:
    """Return whether ``pid`` is a live process.

    Uses ``os.kill(pid, 0)`` (or the injected ``kill`` seam — tests must NOT
    monkeypatch ``os.kill`` globally). Semantics:
    - success or EPERM (PermissionError) → alive (process exists, maybe not ours)
    - ESRCH (ProcessLookupError) → dead
    - ``pid`` is None or ≤ 0 → False
    """
    if pid is None or pid <= 0:
        return False
    kill_fn = kill if kill is not None else os.kill
    try:
        kill_fn(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        raise


# --------------------------------------------------------------------------- #
# Claim leases (FACTORY-PLAN W3 / T13)
# --------------------------------------------------------------------------- #


def lease_dir(watch_abs: Path) -> Path:
    """``.claim/.leases/`` — per-claim liveness records (dot-dir, out of scan/counts)."""
    return Path(watch_abs) / ".claim" / _LEASES_SUBDIR


def lease_path(watch_abs: Path, item_name: str) -> Path:
    """Lease file for a claimed work-item basename."""
    return lease_dir(watch_abs) / item_name


def write_lease(
    watch_abs: Path,
    item_name: str,
    *,
    drain_pid: int,
    child_pid: int | None,
    boot_id: str,
    claimed_at: float,
    ttl_s: int,
    fs: Any = None,
) -> Path:
    """Durably write ``.claim/.leases/<item_name>`` (JSON object, one line).

    Fields: ``drain_pid``, ``child_pid`` (filled once spawned), ``boot_id``,
    ``claimed_at`` (unix epoch), ``ttl_s``.
    """
    path = lease_path(watch_abs, item_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "drain_pid": drain_pid,
        "child_pid": child_pid,
        "boot_id": boot_id,
        "claimed_at": float(claimed_at),
        "ttl_s": int(ttl_s),
    }
    atomic_write_text(path, json.dumps(rec, ensure_ascii=False) + "\n", fs=fs)
    return path


def read_lease(path: Path) -> dict[str, Any]:
    """Parse a lease file; raises ``ValueError`` on missing/corrupt content."""
    path = Path(path)
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"empty lease {path}")
    line = text.splitlines()[0]
    doc = json.loads(line)
    if not isinstance(doc, dict):
        raise ValueError(f"lease {path} is not an object")
    for key in ("drain_pid", "boot_id", "claimed_at", "ttl_s"):
        if key not in doc:
            raise ValueError(f"lease {path} missing {key}")
    return doc


def update_lease_child_pid(
    watch_abs: Path,
    item_name: str,
    child_pid: int,
    *,
    fs: Any = None,
) -> None:
    """Fill ``child_pid`` on an existing lease after spawn (mirrors pointer update)."""
    path = lease_path(watch_abs, item_name)
    if not path.is_file():
        return
    try:
        rec = read_lease(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return
    write_lease(
        watch_abs,
        item_name,
        drain_pid=int(rec["drain_pid"]),
        child_pid=int(child_pid),
        boot_id=str(rec["boot_id"]),
        claimed_at=float(rec["claimed_at"]),
        ttl_s=int(rec["ttl_s"]),
        fs=fs,
    )


def clear_lease(watch_abs: Path, item_name: str, *, fs: Any = None) -> None:
    """Best-effort drop of the lease file (terminal retire / unclaim reap)."""
    path = lease_path(watch_abs, item_name)
    try:
        durable_unlink(path, fs=fs)
    except FileNotFoundError:
        pass


def effective_lease_ttl(lease_ttl_s: int, concurrency: int) -> int | None:
    """Resolve trigger lease config → ttl seconds, or None when leases are off.

    - ``LEASE_TTL_OFF`` (0) → disabled (``lease: off``)
    - positive → explicit ttl (always on)
    - ``LEASE_TTL_DEFAULT`` (-1) → on with :data:`DEFAULT_LEASE_TTL_S` iff
      ``concurrency > 1``; serial default stays stuck-forever (D7)
    """
    if lease_ttl_s == LEASE_TTL_OFF:
        return None
    if lease_ttl_s > 0:
        return int(lease_ttl_s)
    # Default policy.
    if concurrency > 1:
        return DEFAULT_LEASE_TTL_S
    return None


def child_is_dead(
    lease: dict[str, Any],
    *,
    current_boot_id: str,
    kill: Callable[[int, int], None] | None = None,
) -> bool:
    """Whether a claim is reapable (holder dead) for lease-reap purposes (T13 C1).

    Uses the full lease (``drain_pid`` + ``child_pid`` + ``boot_id``), not just
    the child. ``child_pid is None`` is the entire claim→spawn window
    (write_lease → mint → pointer → spawn → update_lease_child_pid), not only
    "never spawned" — a concurrent sweep must not reap a live drain mid-mint.

    Four-case table (same boot; different ``boot_id`` → always dead):

    1. ``child_pid`` PRESENT + alive → live run → **not dead** (flag, never reap)
    2. ``child_pid`` PRESENT + dead → orphan child → **dead** (reap via T4)
    3. ``child_pid`` None + ``drain_pid`` ALIVE → drain still minting/about to
       spawn → **not dead** (flag — same path as "child ALIVE")
    4. ``child_pid`` None + ``drain_pid`` DEAD → drain crashed pre-spawn →
       **dead** (reap → inbox, no validated run)

    When ``boot_id`` is :data:`BOOT_ID_UNKNOWN` on either side, reboot detection
    is skipped and we fall through to ``pid_alive`` (fails safe — M1: weaker
    coverage, never forces a false reap on boot mismatch alone).
    """
    lease_boot = str(lease.get("boot_id") or "")
    if (
        lease_boot
        and lease_boot != BOOT_ID_UNKNOWN
        and current_boot_id != BOOT_ID_UNKNOWN
        and lease_boot != current_boot_id
    ):
        return True  # machine rebooted → every pid from the old boot is dead

    child = lease.get("child_pid")
    if child is not None:
        try:
            child_i = int(child)
        except (TypeError, ValueError):
            child_i = None
        if child_i is not None and child_i > 0:
            # Cases 1–2: child was spawned — its liveness decides.
            return not pid_alive(child_i, kill=kill)
        # Unparseable / non-positive child_pid: fall through to drain_pid.

    # Cases 3–4: not yet spawned (or spawn record corrupt). Drain process is
    # the liveness signal for the claim→spawn window.
    drain = lease.get("drain_pid")
    try:
        drain_i = int(drain) if drain is not None else None
    except (TypeError, ValueError):
        drain_i = None
    if drain_i is None or drain_i <= 0:
        # No drain to consult — treat as dead (malformed lease / crash before
        # drain_pid was recorded, which cannot happen on the write_lease path).
        return True
    return not pid_alive(drain_i, kill=kill)


def _validated_run_dir(run_dir: Path | str | None) -> Path | None:
    """Return ``run_dir`` if it holds a validated ``run.json``, else None."""
    if not run_dir:
        return None
    path = Path(run_dir)
    try:
        load_run(path)
    except (OSError, ValueError, ConfigError, json.JSONDecodeError):
        return None
    return path


def reap_expired_leases(
    watch_abs: Path,
    *,
    on_done: str,
    now: float | None = None,
    current_boot_id: str | None = None,
    kill: Callable[[int, int], None] | None = None,
    fs: Any = None,
) -> tuple[list[Path], list[Path], list[str]]:
    """Reap expired+dead claims under ``.claim/`` (lease-enabled watch dirs only).

    Caller only invokes this when the trigger has leases enabled. Decision table
    (FACTORY-PLAN T4 + T13 r1 — uses full lease liveness, not child_pid alone):

    - missing lease under lease-enabled → stuck (surface, never auto-reap)
    - not expired → leave
    - expired + holder ALIVE (see :func:`child_is_dead` 4-case table) → FLAG only
      (never kill, never reap). Includes: child alive; **and** child_pid=None
      with live drain_pid (claim→spawn window — C1).
    - expired + holder DEAD (child dead, or child_pid=None with dead drain_pid,
      or different boot_id) → reap:
        - validated ``run.json`` → move to ``.waiting/`` for resume as
          needs_human/exit-6 (intentional conservative class — M2: liveness was
          uncertain, so force human/resume review rather than auto-drive)
        - no validated run → unclaim back to inbox (never ran)

    Per-claim cost is one lease-file read + up to two ``pid_alive`` probes (M3);
    bounded by live WIP depth, acceptable at factory scale.

    When ``current_boot_id`` (or the process cache) is :data:`BOOT_ID_UNKNOWN`,
    reboot detection is skipped (M1 — fails safe, weaker coverage).

    Returns ``(reaped_paths, flagged_live_paths, diagnostics)``.
    """
    watch_abs = Path(watch_abs)
    now_ts = time.time() if now is None else float(now)
    cur_boot = current_boot_id if current_boot_id is not None else boot_id()
    reaped: list[Path] = []
    flagged: list[Path] = []
    diagnostics: list[str] = []

    claim_dir = watch_abs / ".claim"
    if not claim_dir.is_dir():
        return reaped, flagged, diagnostics

    if cur_boot == BOOT_ID_UNKNOWN:
        # M1: surface silent degradation once per pass (not per claim).
        diagnostics.append(
            "boot_id unresolved (unknown) — pid-reuse-across-reboot detection "
            "disabled; reap falls back to pid_alive only"
        )

    for item in sorted(p for p in claim_dir.iterdir() if p.is_file()):
        name = item.name
        lpath = lease_path(watch_abs, name)
        if not lpath.is_file():
            diagnostics.append(
                f"claim {name}: missing lease under lease-enabled trigger — "
                f"stuck (never auto-reaped)"
            )
            continue
        try:
            lease = read_lease(lpath)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            diagnostics.append(
                f"claim {name}: unreadable lease ({exc}) — stuck (never auto-reaped)"
            )
            continue

        claimed_at = float(lease["claimed_at"])
        ttl_s = int(lease["ttl_s"])
        age = now_ts - claimed_at
        if age <= ttl_s:
            continue  # not expired

        if not child_is_dead(lease, current_boot_id=cur_boot, kill=kill):
            flagged.append(item)
            child = lease.get("child_pid")
            drain = lease.get("drain_pid")
            if child is None:
                reason = (
                    f"child not yet spawned and drain pid {drain} still alive "
                    f"(claim→spawn window) — flagged, not reaped"
                )
            else:
                reason = (
                    f"child pid {child} still alive — flagged, not reaped"
                )
            diagnostics.append(
                f"claim {name}: lease expired (age={age:.0f}s > ttl={ttl_s}s) but {reason}"
            )
            continue

        # Reap: T4 resume-or-requeue.
        run_dir_s: str | None = None
        ptr = pointer_path(claim_dir, name)
        exit_code: int | None = None
        child_pid_rec: int | None = None
        if ptr.is_file():
            try:
                rec = read_pointer(ptr)
                run_dir_s = rec.get("run_dir") or None
                ec = rec.get("exit_code")
                exit_code = ec if isinstance(ec, int) else None
                cp = rec.get("child_pid")
                child_pid_rec = int(cp) if isinstance(cp, int) else None
            except (OSError, ValueError, json.JSONDecodeError):
                run_dir_s = None

        validated = _validated_run_dir(run_dir_s)
        try:
            if validated is not None:
                # Mid-run / parked: move to .waiting for resume (T4).
                # needs_human/exit-6 is intentional (M2) — force resume/review
                # rather than guessing capacity/blocked when liveness was lost.
                dest = retire(
                    watch_abs,
                    item,
                    outcome=RunOutcome(
                        outcome=OutcomeClass.WAITING, waiting_kind="needs_human"
                    ),
                    on_done=on_done,
                    exit_code=exit_code if exit_code is not None else 6,
                    child_pid=child_pid_rec,
                    run_dir=validated,
                    fs=fs,
                )
                clear_lease(watch_abs, name, fs=fs)
                if dest is not None:
                    reaped.append(dest)
                diagnostics.append(
                    f"reaped claim {name}: expired+dead → .waiting/ for resume "
                    f"(run.json at {validated})"
                )
            else:
                # Never minted a validated run — return to inbox (T4 redelivery).
                # Drop claim-side pointer if present (husks must not resume).
                if ptr.is_file():
                    try:
                        durable_unlink(ptr, fs=fs)
                    except FileNotFoundError:
                        pass
                clear_lease(watch_abs, name, fs=fs)
                dest = unclaim(watch_abs, item, fs=fs)
                # Free the identity reservation: claim is gone and nothing is in
                # .waiting/, so holding it would only force the requeued item
                # through orphan-grace before re-admit (strict identity).
                parsed = parse_item_name(name)
                if parsed is not None:
                    release_reservation(watch_abs, parsed.identity, fs=fs)
                reaped.append(dest)
                diagnostics.append(
                    f"reaped claim {name}: expired+dead → inbox (no validated run.json)"
                )
        except Exception as exc:  # noqa: BLE001 — per-item isolation
            diagnostics.append(f"reap of claim {name} hazarded: {exc}")

    return reaped, flagged, diagnostics


def mop_stranded_deferred(watch_abs: Path, *, fs: Any = None) -> list[str]:
    """Promote ``.deferred/*`` whose identity has no live claim/waiting (T12 residual).

    When an orphan reservation is released (or a claim reaped without terminal
    retire), a parked newer rev can strand in ``.deferred/`` forever because
    :func:`promote_deferred` only runs on terminal retire. Reconcile (and sweep)
    mop that residual: free-identity deferreds move to the inbox; orphan
    reservations for those identities are released.
    """
    watch_abs = Path(watch_abs)
    deferred = watch_abs / _DEFERRED_SUBDIR
    if not deferred.is_dir():
        return []
    live = _live_identities(watch_abs)
    diags: list[str] = []
    for entry in sorted(p for p in deferred.iterdir() if p.is_file()):
        identity = entry.name
        if identity in live:
            continue
        deferred_item = _read_item_fields(entry)
        if deferred_item is None:
            try:
                durable_unlink(entry, fs=fs)
            except FileNotFoundError:
                pass
            diags.append(f"dropped unreadable stranded deferred {identity}")
            continue
        inbox_name = deferred_item.filename
        dest = watch_abs / inbox_name
        if dest.exists():
            diags.append(
                f"stranded deferred {identity} not promoted: inbox already has {inbox_name}"
            )
            continue
        try:
            durable_move(entry, dest, fs=fs)
        except (FileNotFoundError, FileExistsError) as exc:
            diags.append(f"stranded deferred promote of {identity} failed: {exc}")
            continue
        release_reservation(watch_abs, identity, fs=fs)
        diags.append(
            f"promoted stranded deferred {identity} rev {deferred_item.rev} → inbox {inbox_name}"
        )
    return diags


def lease_status(
    watch_abs: Path,
    *,
    now: float | None = None,
    current_boot_id: str | None = None,
    kill: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Lease ages + expired-live counts for ``trigger list`` (additive, no reaps)."""
    watch_abs = Path(watch_abs)
    now_ts = time.time() if now is None else float(now)
    cur_boot = current_boot_id if current_boot_id is not None else boot_id()
    ages: list[float] = []
    expired_live = 0
    missing = 0
    claim_dir = watch_abs / ".claim"
    if not claim_dir.is_dir():
        return {
            "lease_ages_s": ages,
            "expired_live": 0,
            "missing_lease": 0,
            "lease_count": 0,
        }
    for item in claim_dir.iterdir():
        if not item.is_file():
            continue
        lpath = lease_path(watch_abs, item.name)
        if not lpath.is_file():
            missing += 1
            continue
        try:
            lease = read_lease(lpath)
        except (OSError, ValueError, json.JSONDecodeError):
            missing += 1
            continue
        age = now_ts - float(lease["claimed_at"])
        ages.append(age)
        ttl_s = int(lease["ttl_s"])
        if age > ttl_s and not child_is_dead(
            lease, current_boot_id=cur_boot, kill=kill
        ):
            expired_live += 1
    return {
        "lease_ages_s": ages,
        "expired_live": expired_live,
        "missing_lease": missing,
        "lease_count": len(ages),
    }


# --------------------------------------------------------------------------- #
# Filename grammar (identity:strict) — FACTORY-PLAN §2 T1
# --------------------------------------------------------------------------- #
#
#   p<prio>-<source>-<id>-r<rev>.json
#
#   prio   — single digit 0–9
#   source — [a-z][a-z0-9]*          (lowercase; first hyphen-separated segment)
#   id     — [a-z0-9]([a-z0-9._-]*[a-z0-9])?   (lowercase; may contain hyphens)
#   rev    — [a-z0-9][a-z0-9._-]*    (W4: pure epoch digits under the ``r`` marker)
#
# Identity = ``<source>-<id>`` (lowercased by construction — case-safe; uppercase
# is nonconforming). Traversal names (``..``, ``/``) cannot appear: names come
# from readdir and the grammar rejects anything outside the charset above.
#
# Derived ledger names (all must fit NAME_MAX):
#   reservation  ``.claim/.ids/<identity>``
#   deferred     ``.deferred/<identity>``
#   tombstone    ``.done/tombstones/<identity>-r<rev>``
#   pointer/item full filename (with prio + .json)
#
# Rev ordering: numeric when both revs are pure digits (W4 ``r<epoch>``); when
# either side is non-numeric, comparison is *incomparable* and callers FAIL SAFE
# (admit / keep the arriving item — never skip/delete on doubt).

_ITEM_NAME_RE = re.compile(
    r"^p(?P<prio>[0-9])-"
    r"(?P<source>[a-z][a-z0-9]*)-"
    r"(?P<id>[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)-"
    r"r(?P<rev>[a-z0-9][a-z0-9._-]*)"
    r"\.json$"
)


@dataclass(frozen=True)
class ItemId:
    """Parsed work-item identity from a conforming filename (T1)."""

    prio: int
    source: str
    id: str
    rev: str

    @property
    def identity(self) -> str:
        """Canonical identity key: ``<source>-<id>``."""
        return f"{self.source}-{self.id}"

    @property
    def filename(self) -> str:
        """Canonical inbox filename for this item."""
        return f"p{self.prio}-{self.source}-{self.id}-r{self.rev}.json"

    @property
    def tombstone_name(self) -> str:
        """Tombstone marker basename: ``<identity>-r<rev>`` (no ``.json``)."""
        return f"{self.identity}-r{self.rev}"


def parse_item_name(name: str) -> ItemId | None:
    """Parse a strict work-item filename; return None if nonconforming.

    Rejects uppercase, wrong shape, over-long names (NAME_MAX budget for the
    filename and every derived ledger name), and anything that is not exactly
    ``p<prio>-<source>-<id>-r<rev>.json``. Module-level pure function — no I/O.
    """
    if not name or len(name) > NAME_MAX:
        return None
    # Case-safe: any uppercase letter is nonconforming (identity is lowercased).
    if any(c.isupper() for c in name):
        return None
    m = _ITEM_NAME_RE.fullmatch(name)
    if m is None:
        return None
    item = ItemId(
        prio=int(m.group("prio")),
        source=m.group("source"),
        id=m.group("id"),
        rev=m.group("rev"),
    )
    # Derived ledger names must also fit NAME_MAX (ENAMETOOLONG mid-QTP hazard).
    if len(item.identity) > NAME_MAX or len(item.tombstone_name) > NAME_MAX:
        return None
    return item


# --------------------------------------------------------------------------- #
# Identity reservation / deferred / rejection (T1 — opt-in identity:strict)
# --------------------------------------------------------------------------- #


def reservation_path(watch_abs: Path, identity: str) -> Path:
    """``.claim/.ids/<identity>`` — durable O_EXCL identity reservation."""
    return Path(watch_abs) / ".claim" / _IDS_SUBDIR / identity


def deferred_path(watch_abs: Path, identity: str) -> Path:
    """``.deferred/<identity>`` — one parked newer-rev file per identity."""
    return Path(watch_abs) / _DEFERRED_SUBDIR / identity


def rejected_dir(watch_abs: Path) -> Path:
    """``.rejected/`` quarantine for nonconforming inbox drops (bounded; never claimed)."""
    return Path(watch_abs) / _REJECTED_SUBDIR


def _tombstone_dir(watch_abs: Path) -> Path:
    return Path(watch_abs) / ".done" / _TOMBSTONE_SUBDIR


def _tombstone_revs(watch_abs: Path, identity: str) -> list[str]:
    """All revs among ``.done/tombstones/<identity>-r*`` markers."""
    tomb_dir = _tombstone_dir(watch_abs)
    if not tomb_dir.is_dir():
        return []
    prefix = f"{identity}-r"
    found: list[str] = []
    for p in tomb_dir.iterdir():
        if not p.is_file() or not p.name.startswith(prefix):
            continue
        rev = p.name[len(prefix) :]
        if rev:
            found.append(rev)
    return found


def highest_tombstoned_rev(watch_abs: Path, identity: str) -> str | None:
    """Highest safely-orderable rev among tombstones for ``identity``, or None.

    Pure-digit revs compare numerically (W4 epoch). When no pure-digit revs
    exist, returns one marker for diagnostics only — callers that skip on
    ``<=`` must use :func:`tombstone_covers` (fail-safe) instead of raw ``<=``.
    """
    revs = _tombstone_revs(watch_abs, identity)
    if not revs:
        return None
    best: str | None = None
    for rev in revs:
        if best is None:
            best = rev
            continue
        order = rev_order(rev, best)
        if order is not None and order > 0:
            best = rev
    return best


def tombstone_covers(watch_abs: Path, identity: str, rev: str) -> str | None:
    """Return a covering tombstone rev if ``rev`` is confidently ≤ some marker.

    Fail-safe: when no tombstone can be safely ordered against ``rev`` (or all
    comparable markers are older), return None — caller must ADMIT, never skip.
    """
    for t_rev in _tombstone_revs(watch_abs, identity):
        order = rev_order(rev, t_rev)
        if order is not None and order <= 0:
            return t_rev
    return None


def _live_identities(watch_abs: Path) -> set[str]:
    """Identities with a live item in ``.claim/`` (top-level) or ``.waiting/``."""
    live: set[str] = set()
    for lane in (".claim", ".waiting"):
        d = Path(watch_abs) / lane
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if not p.is_file():
                continue
            item = parse_item_name(p.name)
            if item is not None:
                live.add(item.identity)
    return live


def reserve_identity(watch_abs: Path, identity: str, *, fs: Any = None) -> bool:
    """O_EXCL create ``.claim/.ids/<identity>``. True if reserved, False if taken."""
    path = reservation_path(watch_abs, identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    return exclusive_create(path, "", fs=fs)


def release_reservation(watch_abs: Path, identity: str, *, fs: Any = None) -> None:
    """Drop the durable reservation for ``identity`` if present (terminal retire)."""
    path = reservation_path(watch_abs, identity)
    try:
        durable_unlink(path, fs=fs)
    except FileNotFoundError:
        pass


def release_orphan_reservations(
    watch_abs: Path,
    *,
    now: float | None = None,
    fs: Any = None,
) -> list[str]:
    """Release aged ``.claim/.ids/*`` with no live claim/waiting item (T1).

    Both conditions required (FACTORY-PLAN T1 grace period):
    (a) no live item for the identity in ``.claim/`` or ``.waiting/``, AND
    (b) reservation mtime is older than :data:`RESERVATION_GRACE_S`.

    The grace cleanly separates a live reserve→claim gap (protected — concurrent
    drains must not release a just-created reservation) from a real crash orphan
    (released). ``now`` is an injectable unix timestamp (drain passes
    ``now.timestamp()``); default ``time.time()``. Over-holding is safe.
    """
    watch_abs = Path(watch_abs)
    ids_dir = watch_abs / ".claim" / _IDS_SUBDIR
    if not ids_dir.is_dir():
        return []
    now_ts = time.time() if now is None else float(now)
    live = _live_identities(watch_abs)
    diags: list[str] = []
    for entry in sorted(ids_dir.iterdir()):
        if not entry.is_file():
            continue
        if entry.name in live:
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        age = now_ts - mtime
        if age < RESERVATION_GRACE_S:
            # Fresh reservation — protect reserve→claim gap (C1).
            continue
        try:
            durable_unlink(entry, fs=fs)
        except FileNotFoundError:
            continue
        diags.append(f"released orphan reservation {entry.name}")
    return diags


def _read_item_fields(path: Path) -> ItemId | None:
    """Best-effort ItemId from a parked deferred body's source/id/rev/prio."""
    try:
        raw = path.read_bytes()
        body = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    try:
        prio = int(body["prio"])
        source = str(body["source"])
        id_ = str(body["id"])
        rev = str(body["rev"])
    except (KeyError, TypeError, ValueError):
        return None
    # Rebuild via the grammar so promotion only re-admits conforming items.
    return parse_item_name(f"p{prio}-{source}-{id_}-r{rev}.json")


def rev_order(a: str, b: str) -> int | None:
    """Compare two rev strings: -1 if a<b, 0 if equal, 1 if a>b; None if unsafe.

    Pure-digit revs (W4 ``r<epoch>`` captured digits) compare as integers so
    ``10`` > ``9``. When either side is non-numeric, return None — callers MUST
    fail safe (admit / keep-arriving; never skip or delete on doubt).
    """
    if a == b:
        return 0
    if a.isdigit() and b.isdigit():
        ai, bi = int(a), int(b)
        if ai < bi:
            return -1
        if ai > bi:
            return 1
        return 0
    return None


def rev_is_newer(a: str, b: str) -> bool | None:
    """True if a > b, False if a ≤ b, None if not safely orderable."""
    order = rev_order(a, b)
    if order is None:
        return None
    return order > 0


def park_deferred(
    watch_abs: Path,
    candidate: Path,
    item: ItemId,
    *,
    fs: Any = None,
) -> str:
    """Move ``candidate`` into ``.deferred/<identity>``; newest numeric rev wins.

    Drops the candidate only when an already-parked rev is confidently ≥ arriving
    (numeric). On incomparable revs: replace with arriving (fail-safe — never
    silently delete the new drop as "older").
    """
    watch_abs = Path(watch_abs)
    candidate = Path(candidate)
    dest = deferred_path(watch_abs, item.identity)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        existing = _read_item_fields(dest)
        if existing is not None:
            newer = rev_is_newer(item.rev, existing.rev)
            if newer is False:
                # Confidently not newer (equal or older) — drop arriving.
                try:
                    durable_unlink(candidate, fs=fs)
                except FileNotFoundError:
                    pass
                return (
                    f"deferred drop {candidate.name}: parked rev {existing.rev!r} "
                    f">= arriving {item.rev!r}"
                )
            # newer is True OR None (incomparable) → replace with arriving.
        try:
            durable_unlink(dest, fs=fs)
        except FileNotFoundError:
            pass
    try:
        durable_move(candidate, dest, fs=fs)
    except FileExistsError:
        # Racer recreated dest — drop candidate if still present.
        try:
            durable_unlink(candidate, fs=fs)
        except FileNotFoundError:
            pass
        return f"deferred race on {item.identity}: candidate dropped"
    return f"deferred {candidate.name} → .deferred/{item.identity} (rev {item.rev})"


def promote_deferred(
    watch_abs: Path,
    item: ItemId,
    *,
    fs: Any = None,
) -> str | None:
    """Promote or drop ``.deferred/<identity>`` while reservation is still held.

    Call BEFORE :func:`release_reservation` (plan T1: transfer then release).
    If deferred rev is confidently > retired rev → install into inbox. If
    confidently ≤ retired → drop + tombstone. If incomparable → promote
    (fail-safe admit-on-doubt). Returns a diagnostic or None when no deferred.
    """
    watch_abs = Path(watch_abs)
    parked = deferred_path(watch_abs, item.identity)
    if not parked.is_file():
        return None
    deferred_item = _read_item_fields(parked)
    if deferred_item is None:
        try:
            durable_unlink(parked, fs=fs)
        except FileNotFoundError:
            pass
        return f"dropped deferred {item.identity} (unreadable body)"
    newer = rev_is_newer(deferred_item.rev, item.rev)
    if newer is False:
        # Confidently ≤ retired — already delivered.
        try:
            durable_unlink(parked, fs=fs)
        except FileNotFoundError:
            pass
        _tombstone(watch_abs, deferred_item.tombstone_name, fs=fs)
        return (
            f"dropped deferred {item.identity} "
            f"(rev {deferred_item.rev!r} <= retired {item.rev!r})"
        )
    # newer is True OR None (incomparable) → promote to inbox (fail-safe).
    inbox_name = deferred_item.filename
    dest = watch_abs / inbox_name
    if dest.exists():
        # Inbox already has this name — leave deferred? Prefer drop park if same.
        try:
            durable_unlink(parked, fs=fs)
        except FileNotFoundError:
            pass
        return f"deferred {item.identity} not promoted: inbox already has {inbox_name}"
    try:
        durable_move(parked, dest, fs=fs)
    except (FileNotFoundError, FileExistsError) as exc:
        return f"deferred promote of {item.identity} failed: {exc}"
    return f"promoted deferred {item.identity} rev {deferred_item.rev} → inbox {inbox_name}"


def _reject_candidate(
    watch_abs: Path,
    candidate: Path,
    *,
    reason: str,
    fs: Any = None,
) -> str:
    """Durable-move nonconforming candidate into ``.rejected/``; return diagnostic."""
    watch_abs = Path(watch_abs)
    candidate = Path(candidate)
    rej = rejected_dir(watch_abs)
    rej.mkdir(parents=True, exist_ok=True)
    dest = rej / candidate.name
    # Collision: suffix so we never destroy prior rejection evidence.
    if dest.exists():
        stem, ext = Path(candidate.name).stem, Path(candidate.name).suffix
        n = 2
        while (rej / f"{stem}-v{n}{ext}").exists():
            n += 1
        dest = rej / f"{stem}-v{n}{ext}"
    try:
        durable_move(candidate, dest, fs=fs)
    except FileNotFoundError:
        return f"rejected {candidate.name} vanished before quarantine ({reason})"
    except FileExistsError:
        try:
            durable_unlink(candidate, fs=fs)
        except FileNotFoundError:
            pass
        return f"rejected {candidate.name} already quarantined ({reason})"
    return f"rejected {candidate.name} → .rejected/ ({reason})"


def _body_agrees(body: dict[str, Any], item: ItemId) -> bool:
    """Filename↔body agreement on (source, id, rev, prio)."""
    try:
        if str(body.get("source", "")) != item.source:
            return False
        if str(body.get("id", "")) != item.id:
            return False
        if str(body.get("rev", "")) != item.rev:
            return False
        if int(body["prio"]) != item.prio:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


AdmitDisposition = Literal["admit", "skip", "reject", "defer"]


@dataclass(frozen=True)
class AdmitResult:
    """Outcome of :func:`admit_strict` for one inbox candidate."""

    disposition: AdmitDisposition
    item: ItemId | None = None
    diagnostic: str | None = None


def admit_strict(
    watch_abs: Path,
    candidate: Path,
    *,
    max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
    fs: Any = None,
) -> AdmitResult:
    """Admission envelope + reservation + tombstone dedupe + defer (T1 strict).

    Call BEFORE claim. Never reserves or claims nonconforming input.

    Dispositions:
    - ``admit``  — identity reserved; caller should claim
    - ``skip``   — duplicate of a tombstoned rev (inbox file removed)
    - ``reject`` — nonconforming; moved to ``.rejected/``
    - ``defer``  — identity live; parked under ``.deferred/``
    """
    watch_abs = Path(watch_abs)
    candidate = Path(candidate)
    name = candidate.name

    # --- admission envelope (before any reservation) ---
    if candidate.is_symlink():
        return AdmitResult(
            "reject",
            diagnostic=_reject_candidate(
                watch_abs, candidate, reason="symlink (not a regular file)", fs=fs
            ),
        )
    if not candidate.is_file():
        return AdmitResult(
            "reject",
            diagnostic=_reject_candidate(
                watch_abs, candidate, reason="not a regular file", fs=fs
            ),
        )
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        return AdmitResult(
            "reject",
            diagnostic=_reject_candidate(
                watch_abs, candidate, reason=f"stat failed: {exc}", fs=fs
            ),
        )
    if size > max_item_bytes:
        return AdmitResult(
            "reject",
            diagnostic=_reject_candidate(
                watch_abs,
                candidate,
                reason=f"byte cap exceeded ({size} > {max_item_bytes})",
                fs=fs,
            ),
        )
    # Envelope parse/validate: ANY failure quarantines to .rejected/ (I2 — never
    # leave a hostile file wedging every subsequent drain via uncaught RecursionError).
    try:
        raw = candidate.read_bytes()
        text = raw.decode("utf-8")
        body = json.loads(text)
    except UnicodeDecodeError:
        return AdmitResult(
            "reject",
            diagnostic=_reject_candidate(
                watch_abs, candidate, reason="not UTF-8", fs=fs
            ),
        )
    except json.JSONDecodeError:
        return AdmitResult(
            "reject",
            diagnostic=_reject_candidate(
                watch_abs, candidate, reason="not JSON", fs=fs
            ),
        )
    except RecursionError:
        return AdmitResult(
            "reject",
            diagnostic=_reject_candidate(
                watch_abs, candidate, reason="JSON too deeply nested (RecursionError)", fs=fs
            ),
        )
    except ValueError as exc:
        return AdmitResult(
            "reject",
            diagnostic=_reject_candidate(
                watch_abs, candidate, reason=f"JSON/value error: {exc}", fs=fs
            ),
        )
    except OSError as exc:
        return AdmitResult(
            "reject",
            diagnostic=_reject_candidate(
                watch_abs, candidate, reason=f"read failed: {exc}", fs=fs
            ),
        )
    except Exception as exc:  # noqa: BLE001 — envelope boundary: quarantine anything
        return AdmitResult(
            "reject",
            diagnostic=_reject_candidate(
                watch_abs, candidate, reason=f"parse/validation failed: {type(exc).__name__}: {exc}", fs=fs
            ),
        )
    if not isinstance(body, dict):
        return AdmitResult(
            "reject",
            diagnostic=_reject_candidate(
                watch_abs, candidate, reason="JSON root is not an object", fs=fs
            ),
        )
    item = parse_item_name(name)
    if item is None:
        return AdmitResult(
            "reject",
            diagnostic=_reject_candidate(
                watch_abs, candidate, reason="filename grammar nonconforming", fs=fs
            ),
        )
    if not _body_agrees(body, item):
        return AdmitResult(
            "reject",
            item=item,
            diagnostic=_reject_candidate(
                watch_abs,
                candidate,
                reason="filename↔body disagreement (source/id/rev/prio)",
                fs=fs,
            ),
        )

    # --- tombstone dedupe: only skip when confidently ≤ a tombstone (fail-safe) ---
    covering = tombstone_covers(watch_abs, item.identity, item.rev)
    if covering is not None:
        try:
            durable_unlink(candidate, fs=fs)
        except FileNotFoundError:
            pass
        return AdmitResult(
            "skip",
            item=item,
            diagnostic=(
                f"dedupe skip {name}: rev {item.rev!r} <= tombstoned {covering!r}"
            ),
        )

    # --- durable reservation (closes cross-drain TOCTOU) ---
    if not reserve_identity(watch_abs, item.identity, fs=fs):
        # Identity live — park as deferred (latest rev wins).
        diag = park_deferred(watch_abs, candidate, item, fs=fs)
        return AdmitResult("defer", item=item, diagnostic=diag)

    return AdmitResult("admit", item=item)


def release_identity_on_terminal(
    watch_abs: Path,
    item_name: str,
    *,
    fs: Any = None,
) -> list[str]:
    """On terminal retire: promote/drop deferred FIRST, then release reservation (T1).

    Order is load-bearing (C2): promote while the reservation still covers the
    identity ("transfer ownership, then release") so a concurrent drain cannot
    re-admit between free and promote. No-op when ``item_name`` does not parse
    as a strict identity filename. Returns diagnostics from promote/drop.
    """
    item = parse_item_name(item_name)
    if item is None:
        return []
    # Promote (or drop) under the still-held reservation, THEN release.
    diag = promote_deferred(watch_abs, item, fs=fs)
    release_reservation(watch_abs, item.identity, fs=fs)
    return [diag] if diag else []


# --------------------------------------------------------------------------- #
# Inbox scan
# --------------------------------------------------------------------------- #


def scan_candidates(watch_abs: Path, glob: str) -> list[Path]:
    """Top-level files in ``watch_abs`` matching ``glob``, sorted.

    Never recurses; excludes directories and any name starting with ``.`` (which keeps
    the ``.claim``/``.done``/``.failed``/``.waiting`` ledger dirs below out of the scan by
    construction). Also excludes the upgrade-safety ``ledger-version`` marker (T8) —
    it lives at the watch root but is never a work item. A watch dir that doesn't exist
    yet (no event has landed) scans empty.
    """
    watch_abs = Path(watch_abs)
    if not watch_abs.is_dir():
        return []
    return sorted(
        p
        for p in watch_abs.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and p.name != LEDGER_VERSION_NAME
        and fnmatch.fnmatch(p.name, glob)
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
    """Create an empty tombstone marker under ``.done/tombstones/<name>`` (T1/T3).

    For grammar-conforming item names the marker is ``<identity>-r<rev>`` so re-entry
    can compare revs; arbitrary (identity:off) names keep the full filename.
    """
    item = parse_item_name(name)
    marker = item.tombstone_name if item is not None else name
    tomb_dir = Path(watch_abs) / ".done" / _TOMBSTONE_SUBDIR
    tomb_dir.mkdir(parents=True, exist_ok=True)
    dest = tomb_dir / marker
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
        placed = _place(claim_path, dest_lane, name, fs=fs)
        # Lease covers .claim/ only — drop on park (child has exited by construction).
        clear_lease(watch_abs, name, fs=fs)
        return placed

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
        clear_lease(watch_abs, name, fs=fs)
        # T3 pin-release: terminal ledger placement FIRST, then clear pin.
        if run_dir_s:
            clear_queue_pin(Path(run_dir_s), fs=fs)
        # T1: release identity reservation + promote deferred (terminal only).
        release_identity_on_terminal(watch_abs, name, fs=fs)
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
        clear_lease(watch_abs, name, fs=fs)
        if run_dir_s:
            clear_queue_pin(Path(run_dir_s), fs=fs)
        release_identity_on_terminal(watch_abs, name, fs=fs)
        return None
    placed = _place(claim_path, watch_abs / ".done", name, fs=fs)
    if src_ptr.is_file():
        try:
            durable_unlink(src_ptr, fs=fs)
        except FileNotFoundError:
            pass
    _tombstone(watch_abs, name, fs=fs)
    clear_lease(watch_abs, name, fs=fs)
    # T3 pin-release: terminal ledger placement FIRST, then clear pin.
    if run_dir_s:
        clear_queue_pin(Path(run_dir_s), fs=fs)
    release_identity_on_terminal(watch_abs, name, fs=fs)
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

    Per call: one readdir per lane + a JSON read of every pointer under
    ``.waiting/.runs/`` — never opens trails. Waiting-class splits
    (needs_human / blocked / capacity) come from pointer ``exit_code`` via
    :func:`classify_exit`. Items in ``.waiting/`` whose pointer is missing or
    unclassifiable still count toward ``waiting`` / ``inflight`` / ``wip`` but
    not a specific class.

    **Admission-loop cost:** when caps are set, the drain calls this once per
    candidate (before each claim). Aggregate cost is therefore
    O(candidates × waiting_depth) pointer reads per drain — e.g. 1000 inbox
    items against a 500-deep waiting lane is ~500k reads. Acceptable for the
    single-machine target scale; an incremental count (delta from what this
    drain itself admitted) is a future optimization if a watch dir ever holds
    thousands of waiting items.

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
    """Outcome of one :func:`sweep` pass over a watch dir.

    W3/T13 additive fields (default empty so older callers keep working):
    ``reaped`` (expired+dead claims resumed or requeued), ``flagged_live``
    (expired but child still alive — never killed), ``promoted_deferred``
    (T12 stranded-deferred mop diagnostics).
    """

    moved: tuple[Path, ...] = ()
    left: tuple[Path, ...] = ()
    repaired: tuple[Path, ...] = ()
    diagnostics: tuple[str, ...] = ()
    reaped: tuple[Path, ...] = ()
    flagged_live: tuple[Path, ...] = ()
    promoted_deferred: tuple[str, ...] = ()


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


def sweep(
    watch_abs: Path,
    *,
    on_done: str,
    fs: Any = None,
    now: float | None = None,
    lease_ttl_s: int | None = None,
    current_boot_id: str | None = None,
    kill: Callable[[int, int], None] | None = None,
) -> SweepReport:
    """Advance ledger health for one watch dir (FACTORY-PLAN T6 + T13).

    Order:
    1. **Lease reap** (only when ``lease_ttl_s`` is not None — lease-enabled
       triggers): expired+dead claims → T4 resume-or-requeue; expired+alive
       (including claim→spawn window: child_pid=None + live drain_pid) → flag
       only; missing lease → stuck surface, never auto-reap.
    2. **Pointer repair** across lanes (T5 crash between pointer move and item).
    3. **Waiting advance** from trail evidence (T6).
    4. **Stranded deferred mop** (T12 residual): free-identity ``.deferred/``
       entries promoted to inbox.

    For each waiting item: read its pointer and the run's trail; route by the last
    terminal event — ``run-done`` → retire DONE; failure-class ``run-halt`` → FAILED;
    waiting-class halt → leave. Vanished run dir → FAILED + diagnostic naming gc.

    **Drain-vs-reconcile concurrency (I1 / T13 r1):** reconcile's flock serializes
    reconcile-vs-reconcile only. A drain's ``sweep`` and a concurrent
    ``reconcile_workspace`` may both sweep the same watch dir. That overlap is
    accepted and safe by construction: T6 lost-race discipline covers ledger
    moves; C1 drain_pid liveness closes the live-reap window; reaped mid-run
    items go to ``.waiting/`` and resume under T6's nonblocking run-lock (no
    double-drive). No cross-process admission lock is added (D-doctrine).

    ``now`` / ``current_boot_id`` / ``kill`` are injectable seams for tests (do not
    monkeypatch ``os.kill`` or ``time.time`` globally).

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
    reaped: list[Path] = []
    flagged_live: list[Path] = []
    promoted: list[str] = []

    # --- W3 lease reap (opt-in; serial default triggers pass lease_ttl_s=None) ---
    if lease_ttl_s is not None:
        try:
            r, f, diags = reap_expired_leases(
                watch_abs,
                on_done=on_done,
                now=now,
                current_boot_id=current_boot_id,
                kill=kill,
                fs=fs,
            )
            reaped.extend(r)
            flagged_live.extend(f)
            diagnostics.extend(diags)
        except Exception as exc:  # noqa: BLE001 — never abort the rest of sweep
            diagnostics.append(f"lease reap hazarded: {exc}")

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

    # --- T12 residual: promote stranded .deferred/ for free identities ---
    try:
        for line in mop_stranded_deferred(watch_abs, fs=fs):
            promoted.append(line)
            diagnostics.append(line)
    except Exception as exc:  # noqa: BLE001 — never abort the sweep report
        diagnostics.append(f"stranded-deferred mop hazarded: {exc}")

    return SweepReport(
        moved=tuple(moved),
        left=tuple(left),
        repaired=tuple(repaired),
        diagnostics=tuple(diagnostics),
        reaped=tuple(reaped),
        flagged_live=tuple(flagged_live),
        promoted_deferred=tuple(promoted),
    )
