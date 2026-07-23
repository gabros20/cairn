"""W6 numbered O_EXCL agent-slot pool (cairn.kernel.agent_slots).

W6-T1: local pool. W6-T2: machine.toml, join-by-presence, sub-pools, aging.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from cairn.kernel.agent_slots import (
    DEFAULT_SLOT_MAX_AGE_S,
    acquire_slot,
    effective_executor_cap,
    effective_max_agents,
    ensure_machine_pool_dir,
    free_slot_count,
    load_machine_config,
    machine_pool_active,
    machine_slots_dir,
    machine_toml_path,
    refresh_slot,
    release_slot,
    resolve_slots_dir,
    slot_path,
    slots_dir_for,
    wait_acquire_slot,
)
from cairn.kernel.errors import ConfigError


def _dead_kill(pid: int, sig: int) -> None:
    raise ProcessLookupError(errno.ESRCH, "No such process")


def _alive_kill(pid: int, sig: int) -> None:
    return None


class _Clock:
    """Injected now/sleep — no real wall time."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


# --------------------------------------------------------------------------- #
# acquire / release / free_slot_count
# --------------------------------------------------------------------------- #


def test_acquire_n_slots_distinct_names(tmp_path: Path) -> None:
    clock = _Clock()
    names = []
    for i in range(3):
        name = acquire_slot(
            tmp_path, 3, pid=1000 + i, now=clock.now, kill=_alive_kill
        )
        assert name is not None
        names.append(name)
    assert names == ["slot-0", "slot-1", "slot-2"]
    assert free_slot_count(tmp_path, 3, now=clock.now, kill=_alive_kill) == 0


def test_acquire_returns_none_when_all_live(tmp_path: Path) -> None:
    clock = _Clock()
    assert acquire_slot(tmp_path, 1, pid=1, now=clock.now, kill=_alive_kill) == "slot-0"
    assert acquire_slot(tmp_path, 1, pid=2, now=clock.now, kill=_alive_kill) is None
    assert free_slot_count(tmp_path, 1, kill=_alive_kill) == 0


def test_dead_holder_reaped_and_reacquired(tmp_path: Path) -> None:
    clock = _Clock()
    # Seed a slot held by a "dead" pid via the live path, then probe with dead kill.
    name = acquire_slot(tmp_path, 1, pid=99999, now=clock.now, kill=_alive_kill)
    assert name == "slot-0"
    path = slot_path(tmp_path, name)
    assert path.is_file()
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["pid"] == 99999

    # All live-kill sees full; dead-kill reaps and reclaims.
    assert acquire_slot(tmp_path, 1, pid=42, now=clock.now, kill=_alive_kill) is None
    got = acquire_slot(tmp_path, 1, pid=42, now=clock.now, kill=_dead_kill)
    assert got == "slot-0"
    rec2 = json.loads(path.read_text(encoding="utf-8"))
    assert rec2["pid"] == 42


def test_release_frees_slot_idempotent(tmp_path: Path) -> None:
    clock = _Clock()
    name = acquire_slot(tmp_path, 1, pid=7, now=clock.now, kill=_alive_kill)
    assert name == "slot-0"
    release_slot(tmp_path, name)
    release_slot(tmp_path, name)  # idempotent
    assert free_slot_count(tmp_path, 1, kill=_alive_kill) == 1
    assert acquire_slot(tmp_path, 1, pid=8, now=clock.now, kill=_alive_kill) == "slot-0"


def test_free_slot_count_live_and_dead(tmp_path: Path) -> None:
    clock = _Clock()
    acquire_slot(tmp_path, 2, pid=1, now=clock.now, kill=_alive_kill)
    acquire_slot(tmp_path, 2, pid=2, now=clock.now, kill=_alive_kill)
    assert free_slot_count(tmp_path, 2, kill=_alive_kill) == 0
    # With dead kill, both holders look dead → free count is full N.
    assert free_slot_count(tmp_path, 2, kill=_dead_kill) == 2
    # Partial: only slot-0 reaped via dead, but free_slot_count doesn't mutate.
    assert free_slot_count(tmp_path, 2, kill=_alive_kill) == 0


def test_refresh_slot_bumps_heartbeat(tmp_path: Path) -> None:
    clock = _Clock(t=100.0)
    name = acquire_slot(tmp_path, 1, pid=os.getpid(), now=clock.now)
    path = slot_path(tmp_path, name)
    before = json.loads(path.read_text(encoding="utf-8"))
    assert before["heartbeat"] == 100.0
    clock.t = 150.0
    refresh_slot(tmp_path, name, now=clock.now)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["pid"] == before["pid"]
    assert after["acquired_at"] == before["acquired_at"]
    assert after["heartbeat"] == 150.0


def test_refresh_missing_slot_is_noop(tmp_path: Path) -> None:
    refresh_slot(tmp_path, "slot-0", now=0.0)  # must not raise


def test_corrupt_slot_is_reaped(tmp_path: Path) -> None:
    clock = _Clock()
    path = slot_path(tmp_path, "slot-0")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-json\n", encoding="utf-8")
    name = acquire_slot(tmp_path, 1, pid=1, now=clock.now, kill=_alive_kill)
    assert name == "slot-0"
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["pid"] == 1


# --------------------------------------------------------------------------- #
# wait_acquire_slot — injected clock, no real sleep
# --------------------------------------------------------------------------- #


def test_wait_acquires_immediately_when_free(tmp_path: Path) -> None:
    clock = _Clock()
    name = wait_acquire_slot(
        tmp_path,
        1,
        pid=1,
        wait_s=10.0,
        now=clock.now,
        sleep=clock.sleep,
        kill=_alive_kill,
    )
    assert name == "slot-0"
    assert clock.sleeps == []


def test_wait_expiry_returns_none(tmp_path: Path) -> None:
    clock = _Clock(t=0.0)
    # Hold the only slot with a live pid.
    assert acquire_slot(tmp_path, 1, pid=9, now=0.0, kill=_alive_kill) == "slot-0"
    started: list[bool] = []

    name = wait_acquire_slot(
        tmp_path,
        1,
        pid=1,
        wait_s=1.0,
        now=clock.now,
        sleep=clock.sleep,
        poll_s=0.5,
        kill=_alive_kill,
        on_wait_start=lambda: started.append(True),
    )
    assert name is None
    assert started == [True]
    assert clock.sleeps  # polled at least once (or wait_s=0 edge)
    assert clock.t >= 1.0


def test_wait_succeeds_when_slot_freed_mid_wait(tmp_path: Path) -> None:
    clock = _Clock(t=0.0)
    assert acquire_slot(tmp_path, 1, pid=9, now=0.0, kill=_alive_kill) == "slot-0"

    def sleep_and_maybe_release(s: float) -> None:
        clock.sleep(s)
        if clock.t >= 0.5:
            release_slot(tmp_path, "slot-0")

    name = wait_acquire_slot(
        tmp_path,
        1,
        pid=1,
        wait_s=5.0,
        now=clock.now,
        sleep=sleep_and_maybe_release,
        poll_s=0.5,
        kill=_alive_kill,
    )
    assert name == "slot-0"


def test_wait_rejects_non_positive_poll_s(tmp_path: Path) -> None:
    clock = _Clock()
    for bad in (0, 0.0, -1, -0.25):
        try:
            wait_acquire_slot(
                tmp_path,
                1,
                pid=1,
                wait_s=1.0,
                now=clock.now,
                sleep=clock.sleep,
                poll_s=bad,
                kill=_alive_kill,
            )
        except ValueError as exc:
            assert "poll_s" in str(exc)
        else:
            raise AssertionError(f"expected ValueError for poll_s={bad!r}")


# --------------------------------------------------------------------------- #
# W6-T2 — machine.toml, join-by-presence, sub-pools, aging
# --------------------------------------------------------------------------- #


def test_load_machine_config_absent_is_none(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert load_machine_config() is None
    assert not machine_toml_path().is_file()


def test_load_machine_config_env_home(tmp_path: Path, monkeypatch) -> None:
    xdg = tmp_path / "xdg"
    cairn_home = xdg / "cairn"
    cairn_home.mkdir(parents=True)
    (cairn_home / "machine.toml").write_text(
        "max_agents = 4\nslot_max_age = \"1h\"\n\n"
        "[executor_max_agents]\nclaude = 2\ncodex = 3\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    cfg = load_machine_config()
    assert cfg is not None
    assert cfg.max_agents == 4
    assert cfg.slot_max_age_s == 3600.0
    assert cfg.executor_max_agents == {"claude": 2, "codex": 3}
    assert machine_slots_dir() == cairn_home / "agents"


def test_load_machine_config_default_age(tmp_path: Path) -> None:
    path = tmp_path / "machine.toml"
    path.write_text("max_agents = 8\n", encoding="utf-8")
    cfg = load_machine_config(path)
    assert cfg is not None
    assert cfg.max_agents == 8
    assert cfg.slot_max_age_s == DEFAULT_SLOT_MAX_AGE_S  # 2h — exceeds plausible agent runtime


def test_load_machine_config_no_max_agents_is_none(tmp_path: Path) -> None:
    path = tmp_path / "machine.toml"
    path.write_text("slot_max_age = \"2h\"\n", encoding="utf-8")
    assert load_machine_config(path) is None


def test_load_machine_config_bad_max_agents_raises(tmp_path: Path) -> None:
    path = tmp_path / "machine.toml"
    path.write_text("max_agents = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_machine_config(path)


def test_join_by_presence_local_vs_shared(tmp_path: Path, monkeypatch) -> None:
    xdg = tmp_path / "xdg"
    (xdg / "cairn").mkdir(parents=True)
    (xdg / "cairn" / "machine.toml").write_text("max_agents = 5\n", encoding="utf-8")
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    machine = load_machine_config()
    ws = tmp_path / "ws"

    # Absent machine_pool + machine.toml present → shared.
    assert machine_pool_active(factory_machine_pool=None, machine=machine) is True
    assert resolve_slots_dir(ws, factory_machine_pool=None, machine=machine) == machine_slots_dir()

    # Explicit opt-out → local even with machine.toml.
    assert machine_pool_active(factory_machine_pool=False, machine=machine) is False
    assert resolve_slots_dir(ws, factory_machine_pool=False, machine=machine) == slots_dir_for(ws)

    # Force-join without machine.toml → shared dir (size from workspace elsewhere).
    assert machine_pool_active(factory_machine_pool=True, machine=None) is True
    assert resolve_slots_dir(ws, factory_machine_pool=True, machine=None) == machine_slots_dir()

    # No machine.toml, no force → local (W6-T1).
    assert machine_pool_active(factory_machine_pool=None, machine=None) is False
    assert resolve_slots_dir(ws, factory_machine_pool=None, machine=None) == slots_dir_for(ws)


def test_effective_max_agents_machine_authority(tmp_path: Path) -> None:
    path = tmp_path / "machine.toml"
    path.write_text("max_agents = 8\n", encoding="utf-8")
    machine = load_machine_config(path)
    # Machine pool active → machine.toml wins over workspace max_agents.
    assert (
        effective_max_agents(
            factory_max_agents=2,
            factory_machine_pool=None,
            machine=machine,
        )
        == 8
    )
    # Opt-out → workspace.
    assert (
        effective_max_agents(
            factory_max_agents=2,
            factory_machine_pool=False,
            machine=machine,
        )
        == 2
    )
    # No pool → workspace (or OFF).
    assert (
        effective_max_agents(
            factory_max_agents=None,
            factory_machine_pool=None,
            machine=None,
        )
        is None
    )


def test_per_executor_sub_pool_caps(tmp_path: Path) -> None:
    clock = _Clock()
    # claude cap=2: third waits; codex has its own count under global 4.
    assert (
        acquire_slot(
            tmp_path,
            2,
            pid=1,
            now=clock.now,
            kill=_alive_kill,
            executor="claude",
            global_n=4,
        )
        == "claude/slot-0"
    )
    assert (
        acquire_slot(
            tmp_path,
            2,
            pid=2,
            now=clock.now,
            kill=_alive_kill,
            executor="claude",
            global_n=4,
        )
        == "claude/slot-1"
    )
    assert (
        acquire_slot(
            tmp_path,
            2,
            pid=3,
            now=clock.now,
            kill=_alive_kill,
            executor="claude",
            global_n=4,
        )
        is None
    )
    # Different executor still free under its own sub-pool.
    assert (
        acquire_slot(
            tmp_path,
            2,
            pid=4,
            now=clock.now,
            kill=_alive_kill,
            executor="codex",
            global_n=4,
        )
        == "codex/slot-0"
    )
    assert free_slot_count(
        tmp_path, 2, kill=_alive_kill, executor="claude", global_n=4
    ) == 0
    assert free_slot_count(
        tmp_path, 2, kill=_alive_kill, executor="codex", global_n=4
    ) == 1


def test_global_vs_per_executor_two_level_cap(tmp_path: Path) -> None:
    clock = _Clock()
    # global_n=2, each executor cap=2 → second executor still blocked by global.
    assert (
        acquire_slot(
            tmp_path, 2, pid=1, now=clock.now, kill=_alive_kill,
            executor="claude", global_n=2,
        )
        == "claude/slot-0"
    )
    assert (
        acquire_slot(
            tmp_path, 2, pid=2, now=clock.now, kill=_alive_kill,
            executor="claude", global_n=2,
        )
        == "claude/slot-1"
    )
    # Global full: codex cannot acquire even though its sub-pool has room.
    assert (
        acquire_slot(
            tmp_path, 2, pid=3, now=clock.now, kill=_alive_kill,
            executor="codex", global_n=2,
        )
        is None
    )
    assert free_slot_count(
        tmp_path, 2, kill=_alive_kill, executor="codex", global_n=2
    ) == 0


def test_effective_executor_cap_fallback() -> None:
    from cairn.kernel.agent_slots import MachineConfig

    m = MachineConfig(max_agents=8, executor_max_agents={"claude": 2})
    assert effective_executor_cap("claude", machine=m, global_n=8) == 2
    assert effective_executor_cap("grok", machine=m, global_n=8) == 8
    assert effective_executor_cap("claude", machine=None, global_n=8) == 8


def test_aging_reaps_old_slot_regardless_of_pid(tmp_path: Path) -> None:
    clock = _Clock(t=1000.0)
    # Seed a live-pid slot with an old acquired_at.
    name = acquire_slot(tmp_path, 1, pid=1, now=clock.now, kill=_alive_kill)
    assert name == "slot-0"
    path = slot_path(tmp_path, name)
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["acquired_at"] = 1000.0
    rec["heartbeat"] = 1000.0
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    # Fresh live slot is NOT reaped when within age.
    clock.t = 1000.0 + 60.0  # 1 minute later
    assert (
        acquire_slot(
            tmp_path, 1, pid=2, now=clock.now, kill=_alive_kill, slot_max_age_s=7200.0
        )
        is None
    )

    # Past max age: reaped even though pid looks live.
    clock.t = 1000.0 + 7201.0
    got = acquire_slot(
        tmp_path, 1, pid=99, now=clock.now, kill=_alive_kill, slot_max_age_s=7200.0
    )
    assert got == "slot-0"
    rec2 = json.loads(path.read_text(encoding="utf-8"))
    assert rec2["pid"] == 99


def test_aging_fresh_live_slot_not_reaped(tmp_path: Path) -> None:
    clock = _Clock(t=5000.0)
    assert (
        acquire_slot(
            tmp_path, 1, pid=7, now=clock.now, kill=_alive_kill, slot_max_age_s=7200.0
        )
        == "slot-0"
    )
    clock.t = 5000.0 + 100.0
    assert (
        acquire_slot(
            tmp_path, 1, pid=8, now=clock.now, kill=_alive_kill, slot_max_age_s=7200.0
        )
        is None
    )
    assert free_slot_count(
        tmp_path, 1, now=clock.now, kill=_alive_kill, slot_max_age_s=7200.0
    ) == 0


def test_ensure_machine_pool_dir_creates_when_active(tmp_path: Path, monkeypatch) -> None:
    xdg = tmp_path / "xdg"
    (xdg / "cairn").mkdir(parents=True)
    (xdg / "cairn" / "machine.toml").write_text("max_agents = 3\n", encoding="utf-8")
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    machine = load_machine_config()
    d = ensure_machine_pool_dir(factory_machine_pool=None, machine=machine)
    assert d is not None
    assert d.is_dir()
    assert d == machine_slots_dir()
    # Opt-out → no create.
    assert ensure_machine_pool_dir(factory_machine_pool=False, machine=machine) is None


def test_d7_no_machine_pool_keeps_flat_local_slots(tmp_path: Path) -> None:
    """No machine pool ⇒ flat slot-0 names; existing W6-T1 acquire path unchanged."""
    clock = _Clock()
    name = acquire_slot(tmp_path, 2, pid=1, now=clock.now, kill=_alive_kill)
    assert name == "slot-0"  # flat, not executor/slot-0
    assert (tmp_path / "slot-0").is_file()
    assert free_slot_count(tmp_path, 2, kill=_alive_kill) == 1
