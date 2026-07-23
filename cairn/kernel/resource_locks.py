"""Named resource leases — serialize shared resources across runs/factories (W8 / FACTORY-PLAN §11).

A step (or pipeline) may declare ``locks: [<name>, ...]``. The walker acquires each
named lock around the step so two runs (or two factories) cannot mutate one shared
resource at once. Lock names are either:

* **opaque** strings (any shared resource) — pass through as the lock file name, or
* **structured** ``repo:<path>`` — resolve via ``git rev-parse --git-common-dir`` to a
  **canonical** machine-wide name so ``repo:brease``, ``repo:./brease``, and
  ``repo:/abs/brease`` all map to the same lock.

Locks live in the machine locks dir (sibling of the W6 machine agent-slots dir):
``$XDG_STATE_HOME/cairn/locks`` / ``~/.local/state/cairn/locks`` / ``~/.cairn/locks``.
The primitive is the same O_EXCL file + heartbeat + heartbeat-stale reap as W6 slots
(a dead / leaked holder's lock is reaped; a live heartbeating holder is never force-
broken). Multiple locks acquire in **canonical sort order** (deadlock-free). Waiting
is CAPACITY-class (excluded from step timeout). **RELEASE-BEFORE-PARK**: a parked run
must never hold a lock.

Opt-in: absent ``locks:`` and no concurrent/dark git-touch enforcement path ⇒
byte-identical to today (D7). Slot-pool opt-out never opts out of repo locks.

Tests inject ``fs=`` / ``kill=`` / ``now=`` / ``sleep=`` / ``runner=`` seams — never
monkeypatch ``os.*`` (D10).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairn.kernel.agent_slots import (
    DEFAULT_HEARTBEAT_STALE_S,
    DEFAULT_POLL_S,
    _holder_live,
    _now_ts,
    _reap_if_stale,
    _slot_record,
    cairn_state_home,
)
from cairn.kernel.durafs import atomic_write_text, durable_unlink, exclusive_create
from cairn.kernel.errors import ConfigError
from cairn.kernel.proc import Runner, SubprocessRunner

# Default bound on how long a step waits for a free lock (15 minutes) — CAPACITY-class.
DEFAULT_LOCK_WAIT_S = 900.0

# Hung-holder surface: live holder past this age is *flagged*, never force-broken
# (mirror claim-lease posture). Age-reap still requires heartbeat staleness.
DEFAULT_HUNG_HOLDER_S = 3600.0

# Age threshold used only with heartbeat-staleness for dead-holder reap (same spirit
# as W6 slot aging). Absent aging would leave leaked locks until pid dies.
DEFAULT_LOCK_MAX_AGE_S = 7200.0

_REPO_PREFIX = "repo:"
# Opaque lock names: filesystem-safe (no path separators / nulls).
_OPAQUE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:@+=-]+$")


# --------------------------------------------------------------------------- #
# Machine locks dir (sibling of machine agent slots)
# --------------------------------------------------------------------------- #


def machine_locks_dir() -> Path:
    """Shared machine-wide resource-locks directory (``…/cairn/locks``)."""
    return cairn_state_home() / "locks"


def lock_path(locks_dir: Path, lock_name: str) -> Path:
    """``<locks_dir>/<lock_name>`` — one O_EXCL file per canonical lock name."""
    return Path(locks_dir) / lock_name


# --------------------------------------------------------------------------- #
# Canonical name resolution
# --------------------------------------------------------------------------- #


def git_common_dir(
    path: Path,
    *,
    runner: Runner | None = None,
) -> Path | None:
    """Absolute resolved ``git-common-dir`` for ``path``, or None if not a git repo.

    Uses the injected :class:`Runner` (production: :class:`SubprocessRunner`). Never
    raises on a non-repo / missing git — returns None.
    """
    r = runner if runner is not None else SubprocessRunner()
    path = Path(path)
    try:
        result = r.run(["git", "-C", str(path), "rev-parse", "--git-common-dir"])
    except OSError:
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    if not out:
        return None
    common = Path(out)
    if not common.is_absolute():
        common = (path / common).resolve()
    else:
        common = common.resolve()
    return common


def is_git_repo(path: Path, *, runner: Runner | None = None) -> bool:
    """True when ``path`` is inside a git work tree / has a common-dir."""
    return git_common_dir(path, runner=runner) is not None


def _repo_lock_name(common_dir: Path) -> str:
    """Stable machine-wide lock name for a git common-dir (hash of resolved path)."""
    digest = hashlib.sha256(str(common_dir).encode("utf-8")).hexdigest()[:24]
    return f"repo#{digest}"


def resolve_lock_name(
    name: str,
    *,
    workspace_dir: Path,
    runner: Runner | None = None,
) -> str:
    """Resolve one authored lock name to its canonical machine-wide form.

    * ``repo:<path>`` → ``repo#<hash>`` of the absolute git common-dir.
      Path may be workspace-relative or absolute. Three spellings of the same
      repo always map to the same lock. Path not in a git repo → ConfigError.
    * Opaque names pass through (must be filesystem-safe).
    """
    if not isinstance(name, str) or not name:
        raise ConfigError("lock name must be a non-empty string")
    if name.startswith(_REPO_PREFIX):
        raw_path = name[len(_REPO_PREFIX) :]
        if not raw_path:
            raise ConfigError(f"lock {name!r}: repo: path is empty")
        p = Path(raw_path)
        if not p.is_absolute():
            p = (Path(workspace_dir) / p).resolve()
        else:
            p = p.resolve()
        common = git_common_dir(p, runner=runner)
        if common is None:
            raise ConfigError(
                f"lock {name!r}: path {p} is not inside a git repository "
                f"(git rev-parse --git-common-dir failed)"
            )
        return _repo_lock_name(common)
    if not _OPAQUE_NAME_RE.match(name):
        raise ConfigError(
            f"lock name {name!r} is not filesystem-safe "
            f"(allowed: letters, digits, _ . : @ + = -)"
        )
    return name


def resolve_lock_names(
    names: tuple[str, ...] | list[str],
    *,
    workspace_dir: Path,
    runner: Runner | None = None,
) -> tuple[str, ...]:
    """Resolve + de-dupe + **sort** lock names (deadlock-free acquire order)."""
    resolved: list[str] = []
    seen: set[str] = set()
    for n in names:
        canon = resolve_lock_name(n, workspace_dir=workspace_dir, runner=runner)
        if canon not in seen:
            seen.add(canon)
            resolved.append(canon)
    return tuple(sorted(resolved))


# --------------------------------------------------------------------------- #
# Acquire / release / refresh / wait (O_EXCL + heartbeat-stale reap)
# --------------------------------------------------------------------------- #


def try_acquire_lock(
    locks_dir: Path,
    lock_name: str,
    *,
    pid: int,
    now: float | Callable[[], float],
    fs: Any = None,
    kill: Callable[[int, int], None] | None = None,
    lock_max_age_s: float | None = DEFAULT_LOCK_MAX_AGE_S,
    heartbeat_stale_s: float | None = DEFAULT_HEARTBEAT_STALE_S,
) -> bool:
    """Try once to claim ``lock_name`` via O_EXCL. True when acquired.

    A lock whose holder pid is dead, whose content is corrupt, or that is aged
    **and** heartbeat-stale is reaped and reacquired. A live heartbeating holder
    is never force-broken (hung-holder is a surface flag only).
    """
    locks_dir = Path(locks_dir)
    locks_dir.mkdir(parents=True, exist_ok=True)
    now_ts = _now_ts(now)
    body = _slot_record(pid, now_ts)
    path = lock_path(locks_dir, lock_name)
    if path.is_file():
        if not _reap_if_stale(
            path,
            kill=kill,
            fs=fs,
            now_ts=now_ts,
            slot_max_age_s=lock_max_age_s,
            heartbeat_stale_s=heartbeat_stale_s,
        ):
            return False  # live holder
    return exclusive_create(path, body, fs=fs)


def release_lock(
    locks_dir: Path,
    lock_name: str,
    *,
    fs: Any = None,
) -> None:
    """Drop a held lock (idempotent — missing path is fine)."""
    path = lock_path(Path(locks_dir), lock_name)
    try:
        durable_unlink(path, fs=fs)
    except FileNotFoundError:
        return
    except OSError:
        return


def refresh_lock(
    locks_dir: Path,
    lock_name: str,
    *,
    now: float | Callable[[], float],
    fs: Any = None,
) -> None:
    """Bump the heartbeat timestamp on a held lock (walker heartbeat loop)."""
    path = lock_path(Path(locks_dir), lock_name)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if not text:
        return
    try:
        rec = json.loads(text.splitlines()[0])
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(rec, dict) or "pid" not in rec:
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


def wait_acquire_lock(
    locks_dir: Path,
    lock_name: str,
    *,
    pid: int,
    wait_s: float,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    poll_s: float = DEFAULT_POLL_S,
    fs: Any = None,
    kill: Callable[[int, int], None] | None = None,
    on_wait_start: Callable[[], None] | None = None,
    lock_max_age_s: float | None = DEFAULT_LOCK_MAX_AGE_S,
    heartbeat_stale_s: float | None = DEFAULT_HEARTBEAT_STALE_S,
) -> bool:
    """Acquire one lock, polling until success or ``wait_s`` elapses.

    Wait clock is the injected ``now``/``sleep`` pair — never real wall time unless
    the caller passes ``time.time`` / ``time.sleep``. Returns True when acquired,
    False on wait expiry. Emits ``on_wait_start`` at most once.
    """
    base_poll = float(poll_s)
    if base_poll <= 0:
        raise ValueError(f"poll_s must be positive, got {poll_s!r}")

    if try_acquire_lock(
        locks_dir,
        lock_name,
        pid=pid,
        now=now,
        fs=fs,
        kill=kill,
        lock_max_age_s=lock_max_age_s,
        heartbeat_stale_s=heartbeat_stale_s,
    ):
        return True

    deadline = now() + max(0.0, float(wait_s))
    if on_wait_start is not None:
        on_wait_start()

    while now() < deadline:
        remaining = deadline - now()
        if remaining <= 0:
            break
        sleep(min(base_poll, remaining))
        if try_acquire_lock(
            locks_dir,
            lock_name,
            pid=pid,
            now=now,
            fs=fs,
            kill=kill,
            lock_max_age_s=lock_max_age_s,
            heartbeat_stale_s=heartbeat_stale_s,
        ):
            return True
    return False


def wait_acquire_locks(
    locks_dir: Path,
    lock_names: tuple[str, ...] | list[str],
    *,
    pid: int,
    wait_s: float,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    poll_s: float = DEFAULT_POLL_S,
    fs: Any = None,
    kill: Callable[[int, int], None] | None = None,
    on_wait_start: Callable[[str], None] | None = None,
    lock_max_age_s: float | None = DEFAULT_LOCK_MAX_AGE_S,
    heartbeat_stale_s: float | None = DEFAULT_HEARTBEAT_STALE_S,
) -> tuple[str, ...]:
    """Acquire every lock in **sorted** order (caller should pre-sort).

    On failure (wait expiry on any lock), releases all already-acquired locks
    and returns an empty tuple. Success returns the acquired names (same order).
    """
    # Always sort — deadlock-free even if caller forgot.
    ordered = tuple(sorted(lock_names))
    held: list[str] = []
    for name in ordered:
        waited = {"emitted": False}

        def _start(n: str = name) -> None:
            if waited["emitted"]:
                return
            waited["emitted"] = True
            if on_wait_start is not None:
                on_wait_start(n)

        ok = wait_acquire_lock(
            locks_dir,
            name,
            pid=pid,
            wait_s=wait_s,
            now=now,
            sleep=sleep,
            poll_s=poll_s,
            fs=fs,
            kill=kill,
            on_wait_start=_start if on_wait_start is not None else None,
            lock_max_age_s=lock_max_age_s,
            heartbeat_stale_s=heartbeat_stale_s,
        )
        if not ok:
            for h in reversed(held):
                release_lock(locks_dir, h, fs=fs)
            return ()
        held.append(name)
    return tuple(held)


def release_locks(
    locks_dir: Path,
    lock_names: tuple[str, ...] | list[str],
    *,
    fs: Any = None,
) -> None:
    """Release every named lock (idempotent). Reverse order of acquire is fine."""
    for name in reversed(list(lock_names)):
        release_lock(locks_dir, name, fs=fs)


# --------------------------------------------------------------------------- #
# Hung-holder surface
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LockStatus:
    """One held lock as the hung-holder / lock-status surface reports it."""

    name: str
    pid: int
    acquired_at: float
    heartbeat: float
    age_s: float
    hung: bool  # live holder past hung-holder ttl — flagged, never force-broken
    live: bool


def list_locks(
    locks_dir: Path | None = None,
    *,
    now: float | Callable[[], float] | None = None,
    kill: Callable[[int, int], None] | None = None,
    hung_holder_s: float = DEFAULT_HUNG_HOLDER_S,
    lock_max_age_s: float | None = DEFAULT_LOCK_MAX_AGE_S,
    heartbeat_stale_s: float | None = DEFAULT_HEARTBEAT_STALE_S,
) -> list[LockStatus]:
    """List held locks under ``locks_dir`` (default: machine locks dir).

    A lock whose holder is live but ``age_s > hung_holder_s`` is flagged ``hung=True``
    — never force-broken. Dead / stale locks are still listed if present (operator
    visibility); reap happens on the next acquire.
    """
    root = Path(locks_dir) if locks_dir is not None else machine_locks_dir()
    if not root.is_dir():
        return []
    now_ts = _now_ts(now)
    out: list[LockStatus] = []
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
            rec = json.loads(text.splitlines()[0]) if text else None
        except (OSError, json.JSONDecodeError, ValueError, IndexError):
            continue
        if not isinstance(rec, dict) or "pid" not in rec:
            continue
        try:
            pid = int(rec["pid"])
            acquired = float(rec.get("acquired_at", 0.0))
            hb = float(rec.get("heartbeat", acquired))
        except (TypeError, ValueError):
            continue
        age = max(0.0, now_ts - acquired)
        live = _holder_live(
            path,
            kill=kill,
            now_ts=now_ts,
            slot_max_age_s=lock_max_age_s,
            heartbeat_stale_s=heartbeat_stale_s,
        )
        hung = bool(live and age > float(hung_holder_s))
        out.append(
            LockStatus(
                name=path.name,
                pid=pid,
                acquired_at=acquired,
                heartbeat=hb,
                age_s=age,
                hung=hung,
                live=live,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Plan / run-entry enforcement (concurrent/dark git-touch without locks)
# --------------------------------------------------------------------------- #


def step_touches_git(
    step: Any,
    *,
    workspace_dir: Path,
    runner: Runner | None = None,
    workspace_is_git: bool | None = None,
) -> bool:
    """Whether ``step`` can mutate a git dir under concurrent/dark execution.

    Definition (FACTORY-PLAN §11 / W8):
    * the workspace root is a git repo **and** the step is ``agent:`` or ``run:``
      (both can mutate via tools / shell), OR
    * the step's effective cwd (today: always under the workspace / run dir) resolves
      under a git common-dir — covered by the workspace-is-git check for current cwd
      policy.

    Manual / gate nodes never count. A docs-only workspace (not a git repo) is never
    flagged.
    """
    kind = getattr(step, "kind", None)
    if kind not in ("agent", "run"):
        return False
    if workspace_is_git is None:
        workspace_is_git = is_git_repo(workspace_dir, runner=runner)
    return bool(workspace_is_git)


def effective_step_locks(
    step: Any,
    *,
    pipeline_locks: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Authored lock names for a step: pipeline-level ∪ step-level (order preserved, de-duped)."""
    step_locks = tuple(getattr(step, "locks", ()) or ())
    if not pipeline_locks and not step_locks:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for n in (*pipeline_locks, *step_locks):
        if n not in seen:
            seen.add(n)
            out.append(n)
    return tuple(out)


def enforce_repo_locks(
    plan: Any,
    *,
    workspace_dir: Path,
    concurrency: int = 1,
    lane: str | None = None,
    runner: Runner | None = None,
    park_lane: str = "lit",
) -> None:
    """Raise :class:`ConfigError` when concurrent/dark git-touch lacks locks/worktree.

    Gate: fires where concurrency + lane are known (run entry / drain preallocate),
    **not** inside pure ``plan()`` — the pipeline file alone cannot see trigger
    concurrency or the selected ``--lane``.

    Fires when:
    * ``concurrency > 1`` **or** a dark lane is selected (``lane`` set and ≠ park lane),
      AND
    * a step touches a git dir (see :func:`step_touches_git`),
      AND
    * that step has neither ``locks:`` nor (W8-T2) a worktree pattern.

    Docs-only (workspace not a git repo) never errors. Concurrent/dark + locks → ok.
    """
    dark = lane is not None and lane != park_lane
    if int(concurrency) <= 1 and not dark:
        return

    workspace_dir = Path(workspace_dir)
    ws_git = is_git_repo(workspace_dir, runner=runner)
    if not ws_git:
        return  # docs-only / non-git workspace — never enforce

    pipeline_locks = tuple(getattr(plan, "pipeline_locks", ()) or ())

    def _walk_nodes(nodes: Any) -> Any:
        for node in nodes:
            kind = type(node).__name__
            if kind == "StepNode" or getattr(node, "kind", None) in ("agent", "run", "manual"):
                yield node
            elif hasattr(node, "steps"):  # ParallelNode
                yield from _walk_nodes(node.steps)
            elif hasattr(node, "body"):  # LoopNode
                yield from _walk_nodes(node.body)

    for step in _walk_nodes(getattr(plan, "nodes", ()) or ()):
        if not step_touches_git(
            step, workspace_dir=workspace_dir, runner=runner, workspace_is_git=ws_git
        ):
            continue
        locks = effective_step_locks(step, pipeline_locks=pipeline_locks)
        # W8-T2 worktree pattern not yet built — locks: is the only escape.
        if locks:
            continue
        sid = getattr(step, "id", "?")
        raise ConfigError(
            f"step '{sid}' mutates a git repo under concurrency/dark without a lock — "
            f"add locks: [repo:<path>] or use a per-run worktree."
        )
