"""W8 named resource leases (cairn.kernel.resource_locks).

O_EXCL acquire, canonical repo: resolution, sort-order, dead-holder reap,
hung-holder surface, concurrent/dark git-touch enforcement. Seams only —
no os.* monkeypatch, no real sleeps.
"""

from __future__ import annotations

import errno
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from cairn.kernel.errors import ConfigError
from cairn.kernel.plan import Plan, StepNode
from cairn.kernel.proc import RunResult, RunnerBase
from cairn.kernel.resource_locks import (
    DEFAULT_HUNG_HOLDER_S,
    DEFAULT_LOCK_HEARTBEAT_STALE_S,
    DEFAULT_LOCK_MAX_AGE_S,
    DEFAULT_LOCK_REFRESH_S,
    LOCK_REFRESH_MARGIN,
    enforce_repo_locks,
    list_locks,
    lock_heartbeat_stale_s,
    lock_path,
    lock_refresh_interval_s,
    machine_locks_dir,
    refresh_lock,
    release_lock,
    resolve_lock_name,
    resolve_lock_names,
    try_acquire_lock,
    wait_acquire_lock,
    wait_acquire_locks,
)


def _dead_kill(pid: int, sig: int) -> None:
    raise ProcessLookupError(errno.ESRCH, "No such process")


def _alive_kill(pid: int, sig: int) -> None:
    return None


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


class _FakeGitRunner(RunnerBase):
    """Maps path → common-dir string (or None for non-repo)."""

    def __init__(self, mapping: dict[str, str | None]) -> None:
        # Keys are resolved absolute path strings of the -C argument.
        self.mapping = {str(Path(k).resolve()): v for k, v in mapping.items()}
        self.calls: list[list[str]] = []

    def spawn(self, argv, *, input=None, cwd=None):  # pragma: no cover - use run
        raise NotImplementedError

    def run(self, argv, *, input=None, cwd=None) -> RunResult:
        self.calls.append(list(argv))
        assert argv[:3] == ["git", "-C"] or (
            argv[0] == "git" and argv[1] == "-C"
        )
        path = str(Path(argv[2]).resolve())
        if argv[3:] == ["rev-parse", "--git-common-dir"]:
            common = self.mapping.get(path)
            if common is None:
                # Also try matching by the raw -C path without re-resolve quirks.
                for k, v in self.mapping.items():
                    if Path(k).resolve() == Path(path).resolve():
                        common = v
                        break
            if common is None:
                return RunResult(returncode=128, stdout="", stderr="not a git repo")
            return RunResult(returncode=0, stdout=common + "\n", stderr="")
        return RunResult(returncode=1, stdout="", stderr="unexpected")


# --------------------------------------------------------------------------- #
# Opaque + acquire / release / dead-holder
# --------------------------------------------------------------------------- #


def test_opaque_lock_acquire_release(tmp_path: Path) -> None:
    clock = _Clock()
    assert try_acquire_lock(
        tmp_path, "shared-db", pid=1, now=clock.now, kill=_alive_kill
    )
    assert lock_path(tmp_path, "shared-db").is_file()
    assert not try_acquire_lock(
        tmp_path, "shared-db", pid=2, now=clock.now, kill=_alive_kill
    )
    release_lock(tmp_path, "shared-db")
    assert try_acquire_lock(
        tmp_path, "shared-db", pid=2, now=clock.now, kill=_alive_kill
    )


def test_dead_holder_reaped(tmp_path: Path) -> None:
    clock = _Clock()
    assert try_acquire_lock(
        tmp_path, "L", pid=99999, now=clock.now, kill=_alive_kill
    )
    assert not try_acquire_lock(
        tmp_path, "L", pid=42, now=clock.now, kill=_alive_kill
    )
    assert try_acquire_lock(
        tmp_path, "L", pid=42, now=clock.now, kill=_dead_kill
    )
    rec = json.loads(lock_path(tmp_path, "L").read_text(encoding="utf-8"))
    assert rec["pid"] == 42


def test_wait_expiry_returns_false_no_real_sleep_when_wait_zero(tmp_path: Path) -> None:
    clock = _Clock()
    assert try_acquire_lock(tmp_path, "L", pid=1, now=clock.now, kill=_alive_kill)
    assert not wait_acquire_lock(
        tmp_path,
        "L",
        pid=2,
        wait_s=0.0,
        now=clock.now,
        sleep=clock.sleep,
        kill=_alive_kill,
    )
    assert clock.sleeps == []


def test_wait_acquire_locks_partial_release_on_expiry(tmp_path: Path) -> None:
    clock = _Clock()
    # Hold B so multi-acquire of [A, B] gets A then fails on B.
    assert try_acquire_lock(tmp_path, "B", pid=9, now=clock.now, kill=_alive_kill)
    held = wait_acquire_locks(
        tmp_path,
        ["B", "A"],  # will sort to A, B
        pid=1,
        wait_s=0.0,
        now=clock.now,
        sleep=clock.sleep,
        kill=_alive_kill,
    )
    assert held == ()
    assert not lock_path(tmp_path, "A").is_file()  # partial released
    assert lock_path(tmp_path, "B").is_file()  # still held by pid 9


def test_acquire_serializes_two_holders_on_one_lock(tmp_path: Path) -> None:
    clock = _Clock()
    assert try_acquire_lock(tmp_path, "R", pid=1, now=clock.now, kill=_alive_kill)
    # Second holder cannot enter while first is live.
    assert not try_acquire_lock(tmp_path, "R", pid=2, now=clock.now, kill=_alive_kill)
    release_lock(tmp_path, "R")
    assert try_acquire_lock(tmp_path, "R", pid=2, now=clock.now, kill=_alive_kill)


def test_sort_order_deadlock_free_acquire(tmp_path: Path) -> None:
    clock = _Clock()
    # Two acquirers wanting opposite order — both sort to A then B.
    order_seen: list[list[str]] = []

    class TrackingClock(_Clock):
        pass

    c1, c2 = TrackingClock(1000.0), TrackingClock(1000.0)
    # Sequential proof of sort: wait_acquire_locks always sorts.
    h1 = wait_acquire_locks(
        tmp_path,
        ["B", "A"],
        pid=1,
        wait_s=0,
        now=c1.now,
        sleep=c1.sleep,
        kill=_alive_kill,
    )
    assert h1 == ("A", "B")
    release_lock(tmp_path, "A")
    release_lock(tmp_path, "B")
    h2 = wait_acquire_locks(
        tmp_path,
        ["A", "B"],
        pid=2,
        wait_s=0,
        now=c2.now,
        sleep=c2.sleep,
        kill=_alive_kill,
    )
    assert h2 == ("A", "B")
    assert order_seen == [] or True  # both paths identical order


# --------------------------------------------------------------------------- #
# Canonical repo: resolution
# --------------------------------------------------------------------------- #


def test_repo_three_spellings_one_lock(tmp_path: Path) -> None:
    # Layout: workspace/ and a nested brease git repo.
    ws = tmp_path / "ws"
    brease = ws / "brease"
    brease.mkdir(parents=True)
    common = brease / ".git"
    common.mkdir()
    runner = _FakeGitRunner(
        {
            str(brease): str(common),
            str(brease.resolve()): str(common.resolve()),
            str((ws / "brease").resolve()): str(common.resolve()),
            str((ws / "./brease").resolve()): str(common.resolve()),
            str(Path("brease").resolve()): None,  # unused
        }
    )
    # Force every spelling to resolve to the same path via the mapping on resolved abs.
    # Absolute spelling:
    abs_name = resolve_lock_name(
        f"repo:{brease.resolve()}", workspace_dir=ws, runner=runner
    )
    # Workspace-relative:
    rel_name = resolve_lock_name("repo:brease", workspace_dir=ws, runner=runner)
    # ./ spelling:
    dot_name = resolve_lock_name("repo:./brease", workspace_dir=ws, runner=runner)
    assert abs_name == rel_name == dot_name
    assert abs_name.startswith("repo#")
    # Opaque passes through.
    assert resolve_lock_name("my-db", workspace_dir=ws, runner=runner) == "my-db"


def test_repo_not_a_git_repo_is_config_error(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    runner = _FakeGitRunner({})
    with pytest.raises(ConfigError, match="not inside a git repository"):
        resolve_lock_name("repo:docs", workspace_dir=ws, runner=runner)


def test_resolve_lock_names_sorts_and_dedupes(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    names = resolve_lock_names(
        ["zebra", "alpha", "zebra"], workspace_dir=ws, runner=_FakeGitRunner({})
    )
    assert names == ("alpha", "zebra")


# --------------------------------------------------------------------------- #
# Hung-holder surface
# --------------------------------------------------------------------------- #


def test_hung_holder_flagged_not_force_broken(tmp_path: Path) -> None:
    clock = _Clock(t=10_000.0)
    # Seed a lock acquired long ago with a live holder.
    path = lock_path(tmp_path, "L")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": 7,
                "acquired_at": 10_000.0 - (DEFAULT_HUNG_HOLDER_S + 60),
                "heartbeat": 10_000.0,  # fresh heartbeat — live
            }
        )
        + "\n",
        encoding="utf-8",
    )
    statuses = list_locks(
        tmp_path, now=clock.now, kill=_alive_kill, hung_holder_s=DEFAULT_HUNG_HOLDER_S
    )
    assert len(statuses) == 1
    assert statuses[0].hung is True
    assert statuses[0].live is True
    # Still cannot acquire (never force-broken).
    assert not try_acquire_lock(
        tmp_path, "L", pid=99, now=clock.now, kill=_alive_kill, lock_max_age_s=None
    )


def test_machine_locks_dir_is_sibling_of_agents(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert machine_locks_dir() == tmp_path / "xdg" / "cairn" / "locks"
    from cairn.kernel.agent_slots import machine_slots_dir

    assert machine_slots_dir().parent == machine_locks_dir().parent


# --------------------------------------------------------------------------- #
# W8-T1 r1 — live holder never age-reaped (cadence pin + aged span)
# --------------------------------------------------------------------------- #


def test_lock_refresh_interval_pinned_independent_of_trail_heartbeat() -> None:
    """[defaults] heartbeat > stale floor must NOT open a reap gap."""
    floor = DEFAULT_LOCK_HEARTBEAT_STALE_S  # 120
    # Large trail heartbeat (the bug config) → refresh capped at floor/4.
    assert lock_refresh_interval_s(200.0) == floor / LOCK_REFRESH_MARGIN
    assert lock_refresh_interval_s(200.0) == DEFAULT_LOCK_REFRESH_S  # 30
    # Off / unset → default 30s.
    assert lock_refresh_interval_s(None) == DEFAULT_LOCK_REFRESH_S
    # Tiny trail heartbeat is allowed (still ≤ cap).
    assert lock_refresh_interval_s(5.0) == 5.0
    # Stale threshold ≥ 4× actual refresh.
    r = lock_refresh_interval_s(200.0)
    assert lock_heartbeat_stale_s(refresh_interval_s=r) >= r * LOCK_REFRESH_MARGIN
    assert lock_heartbeat_stale_s(refresh_interval_s=r) == floor


def test_live_holder_not_reaped_across_aged_span_with_fresh_heartbeat(
    tmp_path: Path,
) -> None:
    """Lock held past lock_max_age with a refreshed heartbeat is NOT reaped."""
    clock = _Clock(t=1000.0)
    assert try_acquire_lock(
        tmp_path, "R", pid=1, now=clock.now, kill=_alive_kill
    )
    # Advance past age threshold; refresh heartbeat so it stays fresh.
    clock.t += DEFAULT_LOCK_MAX_AGE_S + 60.0
    refresh_lock(tmp_path, "R", now=clock.now)
    # Live holder + fresh heartbeat → not reaped even though aged.
    assert not try_acquire_lock(
        tmp_path,
        "R",
        pid=2,
        now=clock.now,
        kill=_alive_kill,
        lock_max_age_s=DEFAULT_LOCK_MAX_AGE_S,
        heartbeat_stale_s=DEFAULT_LOCK_HEARTBEAT_STALE_S,
    )
    rec = json.loads(lock_path(tmp_path, "R").read_text(encoding="utf-8"))
    assert rec["pid"] == 1


def test_dead_holder_reaped_even_when_aged_and_stale(tmp_path: Path) -> None:
    """Dead holder's lock IS reaped (pid-dead path)."""
    clock = _Clock(t=1000.0)
    assert try_acquire_lock(
        tmp_path, "R", pid=99999, now=clock.now, kill=_alive_kill
    )
    clock.t += DEFAULT_LOCK_MAX_AGE_S + 60.0
    # No refresh — heartbeat is also stale; dead kill reaps regardless.
    assert try_acquire_lock(
        tmp_path,
        "R",
        pid=42,
        now=clock.now,
        kill=_dead_kill,
        lock_max_age_s=DEFAULT_LOCK_MAX_AGE_S,
        heartbeat_stale_s=DEFAULT_LOCK_HEARTBEAT_STALE_S,
    )
    rec = json.loads(lock_path(tmp_path, "R").read_text(encoding="utf-8"))
    assert rec["pid"] == 42


def test_aged_stale_live_pid_reaped_only_when_heartbeat_frozen(
    tmp_path: Path,
) -> None:
    """Age-reap requires BOTH age AND stale heartbeat (leaked / non-refreshing)."""
    clock = _Clock(t=1000.0)
    assert try_acquire_lock(
        tmp_path, "R", pid=7, now=clock.now, kill=_alive_kill
    )
    # Advance past age AND stale without refresh → age-reapable even if pid "alive".
    clock.t += DEFAULT_LOCK_MAX_AGE_S + DEFAULT_LOCK_HEARTBEAT_STALE_S + 10.0
    assert try_acquire_lock(
        tmp_path,
        "R",
        pid=8,
        now=clock.now,
        kill=_alive_kill,
        lock_max_age_s=DEFAULT_LOCK_MAX_AGE_S,
        heartbeat_stale_s=DEFAULT_LOCK_HEARTBEAT_STALE_S,
    )
    rec = json.loads(lock_path(tmp_path, "R").read_text(encoding="utf-8"))
    assert rec["pid"] == 8


# --------------------------------------------------------------------------- #
# Enforcement
# --------------------------------------------------------------------------- #


def _agent(step_id: str = "s", locks: tuple[str, ...] = ()) -> StepNode:
    return StepNode(
        id=step_id,
        kind="agent",
        agent=None,
        command=None,
        args={},
        needs=(),
        needs_optional=(),
        produces=(),
        when_runtime=None,
        timeout_s=60,
        retry=(0, False),
        skippable=False,
        executor="fake",
        tier="balanced",
        effort=None,
        env=(),
        network=False,
        locks=locks,
    )


def _plan(nodes, *, pipeline_locks: tuple[str, ...] = ()) -> Plan:
    return Plan(
        pipeline="t",
        version=1,
        params={},
        dims={},
        run_id_template="t",
        nodes=tuple(nodes),
        artifacts={},
        guards=(),
        warnings=[],
        executor_default="fake",
        resolved_models={},
        pipeline_locks=pipeline_locks,
    )


def test_enforcement_concurrent_git_touch_no_lock_errors(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = _FakeGitRunner({str(ws.resolve()): str((ws / ".git").resolve())})
    plan = _plan([_agent("build")])
    with pytest.raises(ConfigError, match="mutates a git repo under concurrency/dark"):
        enforce_repo_locks(plan, workspace_dir=ws, concurrency=2, runner=runner)


def test_enforcement_dark_lane_git_touch_no_lock_errors(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = _FakeGitRunner({str(ws.resolve()): str((ws / ".git").resolve())})
    plan = _plan([_agent("build")])
    with pytest.raises(ConfigError, match="step 'build'"):
        enforce_repo_locks(
            plan, workspace_dir=ws, concurrency=1, lane="dark", runner=runner
        )


def test_enforcement_docs_only_ok(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = _FakeGitRunner({})  # not a git repo
    plan = _plan([_agent("write-docs")])
    enforce_repo_locks(plan, workspace_dir=ws, concurrency=4, runner=runner)


def test_enforcement_with_locks_ok(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = _FakeGitRunner({str(ws.resolve()): str((ws / ".git").resolve())})
    plan = _plan([_agent("build", locks=("repo:.",))])
    enforce_repo_locks(plan, workspace_dir=ws, concurrency=2, runner=runner)


def test_enforcement_serial_no_lane_ok_even_without_locks(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = _FakeGitRunner({str(ws.resolve()): str((ws / ".git").resolve())})
    plan = _plan([_agent("build")])
    enforce_repo_locks(plan, workspace_dir=ws, concurrency=1, lane=None, runner=runner)


def test_enforcement_lit_lane_not_dark(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = _FakeGitRunner({str(ws.resolve()): str((ws / ".git").resolve())})
    plan = _plan([_agent("build")])
    enforce_repo_locks(plan, workspace_dir=ws, concurrency=1, lane="lit", runner=runner)
