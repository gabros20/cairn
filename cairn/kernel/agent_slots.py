"""Numbered O_EXCL agent-slot pool (FACTORY-PLAN §9 / W6-T1).

A workspace-local pool of ``slot-0``..``slot-(N-1)`` under ``state/agents/`` bounds
how many coding-agent CLIs may spawn at once. Acquire one slot to invoke an
agent step; at the cap the walker WAITS (bounded); wait expiry parks
``CAPACITY(8)``. Stale slots (holder pid dead) are reaped on acquire.

**Reap is pid-dead-only** (see :func:`acquire_slot` / :func:`_reap_if_stale`): a
leaked slot whose numeric pid was reused by an unrelated live process is never
reaped and the pool runs *under* cap — the safe direction (throttles more, never
double-runs). W6-T2 machine-pool aging is the intended backstop (reap a slot
older than a max lifetime regardless of pid).

Opt-in: absent ``[factory] max_agents`` ⇒ slots OFF ⇒ unbounded (D7). The machine
pool (``~/.cairn/machine.toml``, per-executor sub-pools) is W6-T2.

Tests inject ``fs=`` / ``kill=`` / ``now=`` / ``sleep=`` seams — never monkeypatch
``os.*`` (D10).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cairn.kernel.durafs import atomic_write_text, durable_unlink, exclusive_create
from cairn.kernel.queue_ledger import pid_alive

# Default bound on how long an agent step waits for a free slot (15 minutes).
DEFAULT_SLOT_WAIT_S = 900.0

# Poll cadence while waiting (fixed; short enough for tests with tiny wait_s).
DEFAULT_POLL_S = 0.25

# Workspace-relative default pool dir (machine-wide pool is W6-T2).
DEFAULT_SLOTS_REL = Path("state") / "agents"


def slots_dir_for(workspace_dir: Path) -> Path:
    """Resolve the workspace-local agent-slots directory."""
    return Path(workspace_dir) / DEFAULT_SLOTS_REL


def slot_path(slots_dir: Path, slot_name: str) -> Path:
    """``<slots_dir>/<slot_name>`` — e.g. ``state/agents/slot-0``."""
    return Path(slots_dir) / slot_name


def slot_name_for(index: int) -> str:
    return f"slot-{index}"


def _slot_record(pid: int, now_ts: float) -> str:
    return (
        json.dumps(
            {"pid": int(pid), "acquired_at": float(now_ts), "heartbeat": float(now_ts)},
            ensure_ascii=False,
        )
        + "\n"
    )


def _read_slot(path: Path) -> dict[str, Any] | None:
    """Parse a slot file; None on missing/corrupt content."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        doc = json.loads(text.splitlines()[0])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(doc, dict) or "pid" not in doc:
        return None
    return doc


def _holder_live(
    path: Path,
    *,
    kill: Callable[[int, int], None] | None,
) -> bool:
    """True when the slot file names a still-live holder pid.

    Missing, corrupt, or unreadable content → False (reapable).

    **Pid-reuse caveat:** liveness is numeric-pid only. If a crashed holder's pid
    is later reused by an unrelated process, this returns True and the slot looks
    held — fail-toward under-cap (safe; never false-reaps a live agent). Heartbeat
    aging for that strand is W6-T2.
    """
    rec = _read_slot(path)
    if rec is None:
        return False
    try:
        pid = int(rec["pid"])
    except (TypeError, ValueError, KeyError):
        return False
    return pid_alive(pid, kill=kill)


def _reap_if_stale(
    path: Path,
    *,
    kill: Callable[[int, int], None] | None,
    fs: Any,
) -> bool:
    """Remove a dead/corrupt slot. True if the path is free afterwards.

    Reap criterion is **pid-dead only** (plus corrupt/missing content) — the
    ``heartbeat`` field is never consulted. A leaked slot whose pid was reused
    by a live process is therefore *not* reaped here; the pool runs under cap
    (safe). W6-T2 machine-pool aging is the backstop for that strand.
    """
    if not path.is_file():
        return True
    if _holder_live(path, kill=kill):
        return False
    try:
        durable_unlink(path, fs=fs)
    except FileNotFoundError:
        return True
    except OSError:
        return not path.is_file()
    return not path.is_file()


def acquire_slot(
    slots_dir: Path,
    n: int,
    *,
    pid: int,
    now: float | Callable[[], float],
    fs: Any = None,
    kill: Callable[[int, int], None] | None = None,
) -> str | None:
    """Try to claim one of ``slot-0``..``slot-(n-1)`` via O_EXCL.

    Returns the acquired slot name, or None when all N are held by LIVE holders.
    A slot whose holder pid is dead (or whose content is corrupt) is reaped and
    reacquired. Caller must ensure ``n >= 1``.

    **Pid-reuse / under-cap caveat:** reap is pid-dead-only. A leaked slot file
    whose recorded pid was reused by an unrelated live process (same-boot reuse
    or another machine sharing the FS) still looks live, so it is never reaped
    and the pool permanently runs under the configured cap — the *safe*
    direction (throttles more, never double-runs a live agent past N). There is
    no heartbeat-staleness reap in W6-T1; W6-T2 machine-pool aging is the
    intended backstop (reap a slot older than a max lifetime regardless of pid).
    """
    if n < 1:
        return None
    slots_dir = Path(slots_dir)
    slots_dir.mkdir(parents=True, exist_ok=True)
    now_ts = float(now() if callable(now) else now)
    body = _slot_record(pid, now_ts)

    for i in range(n):
        name = slot_name_for(i)
        path = slot_path(slots_dir, name)
        if path.is_file():
            if not _reap_if_stale(path, kill=kill, fs=fs):
                continue  # live holder
            # reaped (or raced away) — fall through to exclusive_create
        if exclusive_create(path, body, fs=fs):
            return name
        # Lost the race: another process claimed it between reap and create.
    return None


def release_slot(
    slots_dir: Path,
    slot_name: str,
    *,
    fs: Any = None,
) -> None:
    """Drop a held slot (idempotent — missing path is fine)."""
    path = slot_path(Path(slots_dir), slot_name)
    try:
        durable_unlink(path, fs=fs)
    except FileNotFoundError:
        return
    except OSError:
        return


def refresh_slot(
    slots_dir: Path,
    slot_name: str,
    *,
    now: float | Callable[[], float],
    fs: Any = None,
) -> None:
    """Bump the heartbeat timestamp on a held slot (walker heartbeat loop).

    No-op when the slot file is missing (already released) or unreadable.
    Preserves ``pid`` / ``acquired_at``; only ``heartbeat`` advances.
    """
    path = slot_path(Path(slots_dir), slot_name)
    rec = _read_slot(path)
    if rec is None:
        return
    now_ts = float(now() if callable(now) else now)
    rec["heartbeat"] = float(now_ts)
    try:
        atomic_write_text(
            path,
            json.dumps(rec, ensure_ascii=False) + "\n",
            fs=fs,
        )
    except OSError:
        return


def free_slot_count(
    slots_dir: Path,
    n: int,
    *,
    now: float | Callable[[], float] | None = None,  # reserved for W6-T2 aging
    kill: Callable[[int, int], None] | None = None,
) -> int:
    """How many of ``slot-0``..``slot-(n-1)`` are free (absent or dead holder).

    Live-holder-aware. Exposed for W3 admission / the beat (wiring is W6-T2).
    ``now`` is accepted for API symmetry with acquire; unused today.
    """
    del now  # reserved
    if n < 1:
        return 0
    slots_dir = Path(slots_dir)
    free = 0
    for i in range(n):
        path = slot_path(slots_dir, slot_name_for(i))
        if not path.is_file():
            free += 1
            continue
        if not _holder_live(path, kill=kill):
            free += 1
    return free


def wait_acquire_slot(
    slots_dir: Path,
    n: int,
    *,
    pid: int,
    wait_s: float,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    poll_s: float = DEFAULT_POLL_S,
    fs: Any = None,
    kill: Callable[[int, int], None] | None = None,
    on_wait_start: Callable[[], None] | None = None,
) -> str | None:
    """Acquire a slot, polling until success or ``wait_s`` elapses.

    The wait clock is the injected ``now``/``sleep`` pair — never real wall time
    unless the caller passes ``time.time`` / ``time.sleep``. Returns the slot
    name, or None when the wait expires with no free slot. Emits ``on_wait_start``
    at most once, the first time a poll is needed.

    ``poll_s`` must be positive — ``poll_s <= 0`` would busy-spin a full-pool wait
    (hot CPU) and raises :class:`ValueError`.
    """
    base_poll = float(poll_s)
    if base_poll <= 0:
        raise ValueError(f"poll_s must be positive, got {poll_s!r}")

    slot = acquire_slot(slots_dir, n, pid=pid, now=now, fs=fs, kill=kill)
    if slot is not None:
        return slot

    deadline = now() + max(0.0, float(wait_s))
    if on_wait_start is not None:
        on_wait_start()

    # Cap poll so a short wait_s does not oversleep past the deadline.
    while now() < deadline:
        remaining = deadline - now()
        if remaining <= 0:
            break
        sleep(min(base_poll, remaining))
        slot = acquire_slot(slots_dir, n, pid=pid, now=now, fs=fs, kill=kill)
        if slot is not None:
            return slot
    return None
