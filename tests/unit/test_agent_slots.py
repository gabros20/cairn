"""W6-T1 numbered O_EXCL agent-slot pool (cairn.kernel.agent_slots)."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

from cairn.kernel.agent_slots import (
    acquire_slot,
    free_slot_count,
    refresh_slot,
    release_slot,
    slot_path,
    wait_acquire_slot,
)


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
