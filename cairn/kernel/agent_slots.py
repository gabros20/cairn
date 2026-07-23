"""Numbered O_EXCL agent-slot pool (FACTORY-PLAN §9 / W6).

A pool of ``slot-0``..``slot-(N-1)`` bounds how many coding-agent CLIs may spawn
at once. Acquire one slot to invoke an agent step; at the cap the walker WAITS
(bounded); wait expiry parks ``CAPACITY(8)``. Stale slots (holder pid dead) are
reaped on acquire. W6-T2 adds a machine-wide pool, per-executor sub-pools, and
an aging backstop gated on **heartbeat staleness** (never reaps a live agent).

**Reap criteria** (see :func:`acquire_slot` / :func:`_reap_if_stale`):

1. **Pid-dead** — holder pid is not alive (W6-T1).
2. **Aged + heartbeat-stale** (W6-T2) — BOTH ``acquired_at`` older than
   ``slot_max_age_s`` AND ``heartbeat`` older than
   :data:`DEFAULT_HEARTBEAT_STALE_S`. A live long agent keeps a fresh heartbeat
   via :func:`refresh_slot` during invoke → never reaped. A leaked slot
   (crashed run, or a dead pid reused by an unrelated process that never
   refreshes THIS slot) freezes its heartbeat → aged+stale → reaped. This
   closes the W6-T1 pid-reuse under-cap strand **without** reaping a live
   holder past the per-executor cap.

**Join-by-presence / slots-dir resolution** (see :func:`resolve_slots_dir`):

1. ``[factory] machine_pool = false`` → workspace-local ``state/agents/`` (opt-out).
2. Machine pool in effect (``machine.toml`` with ``max_agents``, OR
   ``[factory] machine_pool = true``) → shared machine dir
   (``$XDG_STATE_HOME/cairn/agents`` / ``~/.local/state/cairn/agents`` /
   ``~/.cairn/agents`` — same XDG ladder as gatekeys).
3. Else → workspace-local ``state/agents/`` (W6-T1).

**Two-level cap (machine pool):** per-executor sub-pool is exact via O_EXCL;
the global total is best-effort / TOCTOU-racy under concurrent acquirers
(no cross-process admission lock — doctrine). Global overshoot is bounded by
the number of concurrent acquirers; the per-executor dimension is the one
that protects vendor rate limits.

Opt-in: absent ``[factory] max_agents`` AND no machine pool ⇒ slots OFF ⇒
unbounded (D7). Slot-pool opt-out NEVER opts out of W8 repo locks (boundary
note only; W8 not built). Cross-machine shared FS is unsupported (local
``pid_alive`` on a remote holder's pid is unsafe).

Tests inject ``fs=`` / ``kill=`` / ``now=`` / ``sleep=`` seams — never monkeypatch
``os.*`` (D10). Machine home is env-injectable via ``XDG_STATE_HOME`` / ``HOME``.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cairn.kernel.config import parse_duration
from cairn.kernel.durafs import atomic_write_text, durable_unlink, exclusive_create
from cairn.kernel.errors import ConfigError
from cairn.kernel.queue_ledger import pid_alive
from cairn.kernel.types import Finding

# Default bound on how long an agent step waits for a free slot (15 minutes).
DEFAULT_SLOT_WAIT_S = 900.0

# Poll cadence while waiting (fixed; short enough for tests with tiny wait_s).
DEFAULT_POLL_S = 0.25

# Workspace-relative default pool dir (machine-wide pool is W6-T2).
DEFAULT_SLOTS_REL = Path("state") / "agents"

# Aging age threshold default: 2 hours (``acquired_at``). Alone it does NOT
# reap — see heartbeat staleness. Configurable via machine.toml ``slot_max_age``
# or ``slot_max_age_s``.
DEFAULT_SLOT_MAX_AGE_S = 7200.0

# Heartbeat-staleness threshold for age-reap. Walker refresh cadence is 30s when
# trail heartbeats are off (``_slot_beat_interval_s``) or the configured
# ``[defaults] heartbeat`` — a few × that interval so a live agent is never
# age-reaped while still refreshing. A leaked slot stops refreshing → stale.
DEFAULT_HEARTBEAT_STALE_S = 120.0  # 4 × 30s slot-only beat

# Per-executor sub-pool table name in machine.toml (TOML cannot have max_agents
# as both an int and a table; brief's ``[max_agents] claude = 2`` is expressed here).
_EXECUTOR_CAPS_KEY = "executor_max_agents"


# --------------------------------------------------------------------------- #
# Machine state home (XDG ladder — same shape as gatekeys)
# --------------------------------------------------------------------------- #


def cairn_state_home() -> Path:
    """User-level cairn state dir (gatekeys / machine.toml / machine agents).

    Location ladder (first that applies):
        ``$XDG_STATE_HOME/cairn``
        ``~/.local/state/cairn``   (HOME set, XDG_STATE_HOME not)
        ``~/.cairn``               (neither set)
    """
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg:
        return Path(xdg) / "cairn"
    home = os.environ.get("HOME", "").strip()
    if home:
        return Path(home) / ".local" / "state" / "cairn"
    return Path.home() / ".cairn"


def machine_toml_path() -> Path:
    """``<cairn_state_home>/machine.toml`` — machine pool authority (absent ⇒ no machine pool)."""
    return cairn_state_home() / "machine.toml"


def machine_slots_dir() -> Path:
    """Shared machine-wide agent-slots directory (``…/cairn/agents``)."""
    return cairn_state_home() / "agents"


# --------------------------------------------------------------------------- #
# machine.toml loader
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MachineConfig:
    """Parsed ``~/.cairn/machine.toml`` (or XDG equivalent).

    ``max_agents`` is the machine-wide total cap. ``executor_max_agents`` is an
    optional per-executor sub-pool (vendor rate limits are per-vendor). When a
    sub-pool is absent for an executor, that executor may use the global
    ``max_agents`` budget (still subject to the two-level total).
    """

    max_agents: int
    executor_max_agents: dict[str, int] = field(default_factory=dict)
    slot_max_age_s: float = DEFAULT_SLOT_MAX_AGE_S
    path: Path | None = None


def load_machine_config(
    path: Path | None = None,
    *,
    warnings: list[Finding] | None = None,
) -> MachineConfig | None:
    """Load machine pool authority. Absent / empty file ⇒ ``None`` (no machine pool).

    ``path`` defaults to :func:`machine_toml_path` (env-injectable home for tests).
    Raises :class:`ConfigError` on malformed content when the file exists and is
    non-empty.
    """
    file = Path(path) if path is not None else machine_toml_path()
    if not file.is_file():
        return None
    try:
        raw_text = file.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw_text.strip():
        return None
    try:
        raw = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"invalid machine.toml at {file}: {exc}",
            findings=[Finding("error", f"invalid machine.toml: {exc}")],
            file=str(file),
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigError(
            f"machine.toml root must be a table at {file}",
            findings=[Finding("error", "machine.toml root must be a table")],
            file=str(file),
        )

    known = {"max_agents", "slot_max_age", "slot_max_age_s", _EXECUTOR_CAPS_KEY}
    if warnings is not None:
        for key in raw:
            if key not in known:
                warnings.append(
                    Finding("warning", f"unknown key in machine.toml: {key!r}")
                )

    if "max_agents" not in raw:
        # File present but no pool size → no machine pool (join-by-presence needs a pool).
        return None
    n = raw["max_agents"]
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ConfigError(
            f"machine.toml max_agents must be a positive integer, got {n!r}",
            findings=[Finding("error", "machine.toml max_agents must be a positive integer")],
            file=str(file),
        )

    executor_caps: dict[str, int] = {}
    caps_raw = raw.get(_EXECUTOR_CAPS_KEY)
    if caps_raw is not None:
        if not isinstance(caps_raw, dict):
            raise ConfigError(
                f"machine.toml [{_EXECUTOR_CAPS_KEY}] must be a table",
                findings=[Finding("error", f"[{_EXECUTOR_CAPS_KEY}] must be a table")],
                file=str(file),
            )
        for name, cap in caps_raw.items():
            if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
                raise ConfigError(
                    f"machine.toml {_EXECUTOR_CAPS_KEY}.{name} must be a positive integer, "
                    f"got {cap!r}",
                    findings=[
                        Finding(
                            "error",
                            f"{_EXECUTOR_CAPS_KEY}.{name} must be a positive integer",
                        )
                    ],
                    file=str(file),
                )
            executor_caps[str(name)] = cap

    age_s = DEFAULT_SLOT_MAX_AGE_S
    if "slot_max_age_s" in raw:
        v = raw["slot_max_age_s"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise ConfigError(
                f"machine.toml slot_max_age_s must be a positive number, got {v!r}",
                findings=[Finding("error", "slot_max_age_s must be a positive number")],
                file=str(file),
            )
        age_s = float(v)
    elif "slot_max_age" in raw:
        v = raw["slot_max_age"]
        if isinstance(v, bool) or isinstance(v, int):
            if isinstance(v, bool) or v <= 0:
                raise ConfigError(
                    f"machine.toml slot_max_age must be a positive duration, got {v!r}",
                    findings=[Finding("error", "slot_max_age must be a positive duration")],
                    file=str(file),
                )
            age_s = float(v)
        elif isinstance(v, str):
            try:
                age_s = float(parse_duration(v))
            except ValueError as exc:
                raise ConfigError(
                    f"machine.toml slot_max_age: {exc}",
                    findings=[Finding("error", f"slot_max_age: {exc}")],
                    file=str(file),
                ) from exc
            if age_s <= 0:
                raise ConfigError(
                    f"machine.toml slot_max_age must be positive, got {v!r}",
                    findings=[Finding("error", "slot_max_age must be positive")],
                    file=str(file),
                )
        else:
            raise ConfigError(
                f"machine.toml slot_max_age must be a duration string or seconds, got {v!r}",
                findings=[Finding("error", "slot_max_age must be a duration")],
                file=str(file),
            )

    return MachineConfig(
        max_agents=n,
        executor_max_agents=executor_caps,
        slot_max_age_s=age_s,
        path=file,
    )


# --------------------------------------------------------------------------- #
# Join-by-presence / effective pool resolution
# --------------------------------------------------------------------------- #


def machine_pool_active(
    *,
    factory_machine_pool: bool | None,
    machine: MachineConfig | None,
) -> bool:
    """Whether this workspace joins the shared machine agent pool.

    Precedence:
    - ``factory_machine_pool is False`` → never (explicit opt-out).
    - ``factory_machine_pool is True`` → yes (shared dir; size from machine.toml
      or falls back to workspace ``max_agents`` at the call site).
    - ``factory_machine_pool is None`` (key absent) → yes iff ``machine.toml``
      is present with a pool (``machine is not None``).
    """
    if factory_machine_pool is False:
        return False
    if factory_machine_pool is True:
        return True
    return machine is not None


def slots_dir_for(workspace_dir: Path) -> Path:
    """Resolve the workspace-local agent-slots directory (W6-T1)."""
    return Path(workspace_dir) / DEFAULT_SLOTS_REL


def resolve_slots_dir(
    workspace_dir: Path,
    *,
    factory_machine_pool: bool | None = None,
    machine: MachineConfig | None = None,
) -> Path:
    """Effective slots directory for this workspace (join-by-presence).

    See module docstring for the full precedence. When the machine pool is
    active the shared machine dir is used; otherwise workspace-local
    ``state/agents/``.
    """
    if machine_pool_active(factory_machine_pool=factory_machine_pool, machine=machine):
        return machine_slots_dir()
    return slots_dir_for(workspace_dir)


def effective_max_agents(
    *,
    factory_max_agents: int | None,
    factory_machine_pool: bool | None,
    machine: MachineConfig | None,
) -> int | None:
    """Pool size for this workspace, or ``None`` when slots are OFF.

    - Machine pool active + machine.toml present → machine ``max_agents`` (authority).
    - Machine pool active via ``machine_pool=true`` without machine.toml →
      workspace ``max_agents`` (must be set or slots stay OFF).
    - Local pool → workspace ``max_agents``.
    """
    if machine_pool_active(factory_machine_pool=factory_machine_pool, machine=machine):
        if machine is not None:
            return machine.max_agents
        return factory_max_agents
    return factory_max_agents


def effective_slot_max_age_s(
    *,
    factory_machine_pool: bool | None,
    machine: MachineConfig | None,
) -> float | None:
    """Aging threshold, or ``None`` when aging is off (workspace-local W6-T1).

    Aging is the W6-T2 backstop and applies when the machine pool is active so
    D7 holds for pure workspace-local pools (no aging unless machine pool).
    """
    if not machine_pool_active(factory_machine_pool=factory_machine_pool, machine=machine):
        return None
    if machine is not None:
        return float(machine.slot_max_age_s)
    return DEFAULT_SLOT_MAX_AGE_S


def effective_executor_cap(
    executor: str | None,
    *,
    machine: MachineConfig | None,
    global_n: int,
) -> int:
    """Per-executor sub-pool size, falling back to ``global_n`` when unset."""
    if executor and machine is not None:
        cap = machine.executor_max_agents.get(executor)
        if cap is not None:
            return min(int(cap), int(global_n))
    return int(global_n)


def ensure_machine_pool_dir(
    *,
    factory_machine_pool: bool | None = None,
    machine: MachineConfig | None = None,
) -> Path | None:
    """Create the shared machine slots dir when the machine pool is in effect.

    Returns the dir path when created/ensured, else ``None``. Used by
    ``trigger sync`` bootstrap so the dir exists before the first drain.
    """
    if not machine_pool_active(factory_machine_pool=factory_machine_pool, machine=machine):
        return None
    d = machine_slots_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
# Slot file primitives
# --------------------------------------------------------------------------- #


def slot_path(slots_dir: Path, slot_name: str) -> Path:
    """``<slots_dir>/<slot_name>`` — e.g. ``state/agents/slot-0`` or ``…/claude/slot-0``."""
    return Path(slots_dir) / slot_name


def slot_name_for(index: int, *, executor: str | None = None) -> str:
    """Flat ``slot-N`` (local) or ``<executor>/slot-N`` (machine sub-pool)."""
    base = f"slot-{index}"
    if executor:
        return f"{executor}/{base}"
    return base


def _slot_record(pid: int, now_ts: float, *, executor: str | None = None) -> str:
    doc: dict[str, Any] = {
        "pid": int(pid),
        "acquired_at": float(now_ts),
        "heartbeat": float(now_ts),
    }
    if executor:
        doc["executor"] = executor
    return json.dumps(doc, ensure_ascii=False) + "\n"


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


def _now_ts(now: float | Callable[[], float] | None) -> float:
    if now is None:
        import time

        return time.time()
    return float(now() if callable(now) else now)


def _slot_aged(
    rec: dict[str, Any],
    *,
    now_ts: float,
    slot_max_age_s: float | None,
) -> bool:
    """True when ``acquired_at`` is older than the aging threshold."""
    if slot_max_age_s is None:
        return False
    try:
        acquired = float(rec.get("acquired_at", 0.0))
    except (TypeError, ValueError):
        return True  # corrupt timestamp → treat as aged
    return (now_ts - acquired) > float(slot_max_age_s)


def _slot_heartbeat_stale(
    rec: dict[str, Any],
    *,
    now_ts: float,
    heartbeat_stale_s: float | None,
) -> bool:
    """True when ``heartbeat`` is older than the staleness threshold."""
    if heartbeat_stale_s is None:
        return False
    try:
        hb = float(rec.get("heartbeat", rec.get("acquired_at", 0.0)))
    except (TypeError, ValueError):
        return True  # corrupt → treat as stale
    return (now_ts - hb) > float(heartbeat_stale_s)


def _slot_age_reapable(
    rec: dict[str, Any],
    *,
    now_ts: float,
    slot_max_age_s: float | None,
    heartbeat_stale_s: float | None,
) -> bool:
    """True when the slot is BOTH aged and heartbeat-stale (W6-T2 r1).

    Never true for a live long agent: :func:`refresh_slot` keeps ``heartbeat``
    fresh, so age alone is not enough to reap. A leaked / pid-reused slot stops
    refreshing → heartbeat freezes → eventually aged+stale → reaped.
    """
    if slot_max_age_s is None:
        return False
    stale_s = (
        float(heartbeat_stale_s)
        if heartbeat_stale_s is not None
        else DEFAULT_HEARTBEAT_STALE_S
    )
    return _slot_aged(
        rec, now_ts=now_ts, slot_max_age_s=slot_max_age_s
    ) and _slot_heartbeat_stale(rec, now_ts=now_ts, heartbeat_stale_s=stale_s)


def _holder_live(
    path: Path,
    *,
    kill: Callable[[int, int], None] | None,
    now_ts: float | None = None,
    slot_max_age_s: float | None = None,
    heartbeat_stale_s: float | None = None,
) -> bool:
    """True when the slot file names a still-held (live or fresh-heartbeat) agent.

    Missing, corrupt, or unreadable content → False (reapable).

    **Age-reap (W6-T2 r1):** only when BOTH ``acquired_at`` is past
    ``slot_max_age_s`` AND ``heartbeat`` is past the staleness threshold. A live
    long agent with a fresh heartbeat is **never** reaped. A leaked slot (or a
    pid-reused process that never refreshes this slot) has a stale heartbeat →
    reaped — closing the W6-T1 pid-reuse under-cap strand without over-cap.

    **Pid-reuse caveat (when aging is off):** liveness is numeric-pid only. If a
    crashed holder's pid is later reused by an unrelated process, this returns
    True and the slot looks held — fail-toward under-cap (safe).
    """
    rec = _read_slot(path)
    if rec is None:
        return False
    if now_ts is not None and _slot_age_reapable(
        rec,
        now_ts=now_ts,
        slot_max_age_s=slot_max_age_s,
        heartbeat_stale_s=heartbeat_stale_s,
    ):
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
    now_ts: float | None = None,
    slot_max_age_s: float | None = None,
    heartbeat_stale_s: float | None = None,
) -> bool:
    """Remove a dead/corrupt/age-reapable slot. True if the path is free afterwards.

    Reap criteria: pid-dead, corrupt/missing content, OR (when aging is on)
    aged **and** heartbeat-stale. A live heartbeating agent is never reaped.
    """
    if not path.is_file():
        return True
    if _holder_live(
        path,
        kill=kill,
        now_ts=now_ts,
        slot_max_age_s=slot_max_age_s,
        heartbeat_stale_s=heartbeat_stale_s,
    ):
        return False
    try:
        durable_unlink(path, fs=fs)
    except FileNotFoundError:
        return True
    except OSError:
        return not path.is_file()
    return not path.is_file()


def _count_live_slots(
    slots_dir: Path,
    *,
    kill: Callable[[int, int], None] | None,
    now_ts: float,
    slot_max_age_s: float | None,
    heartbeat_stale_s: float | None = None,
    executor: str | None = None,
) -> int:
    """Count live (non-reapable) slot files under ``slots_dir`` (optionally one executor)."""
    slots_dir = Path(slots_dir)
    if not slots_dir.is_dir():
        return 0
    live = 0
    if executor is not None:
        root = slots_dir / executor
        if not root.is_dir():
            return 0
        for path in root.iterdir():
            if path.is_file() and path.name.startswith("slot-"):
                if _holder_live(
                    path,
                    kill=kill,
                    now_ts=now_ts,
                    slot_max_age_s=slot_max_age_s,
                    heartbeat_stale_s=heartbeat_stale_s,
                ):
                    live += 1
        return live
    # Global: flat slots + every per-executor subdir.
    for path in slots_dir.iterdir():
        if path.is_file() and path.name.startswith("slot-"):
            if _holder_live(
                path,
                kill=kill,
                now_ts=now_ts,
                slot_max_age_s=slot_max_age_s,
                heartbeat_stale_s=heartbeat_stale_s,
            ):
                live += 1
        elif path.is_dir():
            for child in path.iterdir():
                if child.is_file() and child.name.startswith("slot-"):
                    if _holder_live(
                        child,
                        kill=kill,
                        now_ts=now_ts,
                        slot_max_age_s=slot_max_age_s,
                        heartbeat_stale_s=heartbeat_stale_s,
                    ):
                        live += 1
    return live


def acquire_slot(
    slots_dir: Path,
    n: int,
    *,
    pid: int,
    now: float | Callable[[], float],
    fs: Any = None,
    kill: Callable[[int, int], None] | None = None,
    executor: str | None = None,
    global_n: int | None = None,
    slot_max_age_s: float | None = None,
    heartbeat_stale_s: float | None = None,
) -> str | None:
    """Try to claim one of ``slot-0``..``slot-(n-1)`` via O_EXCL.

    Returns the acquired slot name, or None when all N are held by LIVE holders
    (or the two-level global cap is saturated). A slot whose holder pid is dead,
    whose content is corrupt, or that is aged **and** heartbeat-stale is reaped
    and reacquired. Caller must ensure ``n >= 1``.

    **Executor sub-pools (W6-T2):** when ``executor`` is set, slots live under
    ``<slots_dir>/<executor>/slot-i`` and ``n`` is the per-executor cap (exact
    via O_EXCL). When ``global_n`` is also set, the total live slots across all
    executors should stay ≤ ``global_n`` — **best-effort / TOCTOU-racy** under
    concurrent acquirers (scan-then-create; no cross-process admission lock).
    Global overshoot is bounded by concurrent acquirers; the per-executor
    sub-pool is the exact dimension that protects vendor rate limits.

    **Age-reap (W6-T2 r1):** requires BOTH ``acquired_at > slot_max_age_s`` and
    a stale ``heartbeat`` (default :data:`DEFAULT_HEARTBEAT_STALE_S`). A live
    long agent keeps its heartbeat fresh via :func:`refresh_slot` → never
    reaped. A leaked / pid-reused slot freezes the heartbeat → reaped.
    """
    if n < 1:
        return None
    slots_dir = Path(slots_dir)
    slots_dir.mkdir(parents=True, exist_ok=True)
    now_ts = _now_ts(now)
    body = _slot_record(pid, now_ts, executor=executor)

    # Two-level global cap (best-effort under concurrency — see docstring).
    if global_n is not None and global_n >= 1:
        if (
            _count_live_slots(
                slots_dir,
                kill=kill,
                now_ts=now_ts,
                slot_max_age_s=slot_max_age_s,
                heartbeat_stale_s=heartbeat_stale_s,
            )
            >= global_n
        ):
            return None

    if executor:
        (slots_dir / executor).mkdir(parents=True, exist_ok=True)

    for i in range(n):
        name = slot_name_for(i, executor=executor)
        path = slot_path(slots_dir, name)
        if path.is_file():
            if not _reap_if_stale(
                path,
                kill=kill,
                fs=fs,
                now_ts=now_ts,
                slot_max_age_s=slot_max_age_s,
                heartbeat_stale_s=heartbeat_stale_s,
            ):
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
    Preserves ``pid`` / ``acquired_at`` / ``executor``; only ``heartbeat`` advances.
    """
    path = slot_path(Path(slots_dir), slot_name)
    rec = _read_slot(path)
    if rec is None:
        return
    now_ts = _now_ts(now)
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
    now: float | Callable[[], float] | None = None,
    kill: Callable[[int, int], None] | None = None,
    executor: str | None = None,
    global_n: int | None = None,
    slot_max_age_s: float | None = None,
    heartbeat_stale_s: float | None = None,
) -> int:
    """How many of ``slot-0``..``slot-(n-1)`` are free (absent, dead, or age-reapable).

    Live-holder-aware. Exposed for W3 admission and the capacity-resume beat.
    When ``executor`` is set, counts that sub-pool only. When ``global_n`` is set
    (two-level cap), free count is also bounded by remaining global capacity:
    ``min(per-executor free, global_n − live_total)`` — global side is
    best-effort under concurrency (same TOCTOU as :func:`acquire_slot`).
    """
    if n < 1:
        return 0
    slots_dir = Path(slots_dir)
    now_ts = _now_ts(now)
    free = 0
    for i in range(n):
        path = slot_path(slots_dir, slot_name_for(i, executor=executor))
        if not path.is_file():
            free += 1
            continue
        if not _holder_live(
            path,
            kill=kill,
            now_ts=now_ts,
            slot_max_age_s=slot_max_age_s,
            heartbeat_stale_s=heartbeat_stale_s,
        ):
            free += 1
    if global_n is not None and global_n >= 1:
        live_total = _count_live_slots(
            slots_dir,
            kill=kill,
            now_ts=now_ts,
            slot_max_age_s=slot_max_age_s,
            heartbeat_stale_s=heartbeat_stale_s,
        )
        global_free = max(0, int(global_n) - live_total)
        free = min(free, global_free)
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
    executor: str | None = None,
    global_n: int | None = None,
    slot_max_age_s: float | None = None,
    heartbeat_stale_s: float | None = None,
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

    slot = acquire_slot(
        slots_dir,
        n,
        pid=pid,
        now=now,
        fs=fs,
        kill=kill,
        executor=executor,
        global_n=global_n,
        slot_max_age_s=slot_max_age_s,
        heartbeat_stale_s=heartbeat_stale_s,
    )
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
        slot = acquire_slot(
            slots_dir,
            n,
            pid=pid,
            now=now,
            fs=fs,
            kill=kill,
            executor=executor,
            global_n=global_n,
            slot_max_age_s=slot_max_age_s,
            heartbeat_stale_s=heartbeat_stale_s,
        )
        if slot is not None:
            return slot
    return None
