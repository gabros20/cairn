"""W3/T13 claim leases + liveness helpers + factory reconcile."""

from __future__ import annotations

import errno
import io
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from cairn.kernel.errors import ConfigError
from cairn.kernel.queue_drain import reconcile_workspace, run_trigger
from cairn.kernel.queue_ledger import (
    BOOT_ID_UNKNOWN,
    DEFAULT_LEASE_TTL_S,
    LEASE_TTL_DEFAULT,
    LEASE_TTL_OFF,
    boot_id,
    child_is_dead,
    claim,
    effective_lease_ttl,
    lease_path,
    mop_stranded_deferred,
    pid_alive,
    pointer_path,
    read_lease,
    reap_expired_leases,
    release_reservation,
    reserve_identity,
    sweep,
    update_lease_child_pid,
    write_lease,
    write_pointer,
)
from cairn.kernel.runstate import create_run
from cairn.kernel.trigger_host import load_triggers
from cairn.kernel.types import OutcomeClass, RunOutcome

NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)
NOW_TS = NOW.timestamp()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _payload(run_id: str = "lease-run") -> dict:
    return {
        "run_id": run_id,
        "pipeline": "p",
        "pipeline_hash": "sha256:abc",
        "cairn_version": "0.1.0",
        "params": {},
        "dims": {},
        "executors": {"default": "stub"},
        "models": {},
        "created_at": "2026-07-23T12:00:00.000Z",
        "status": "running",
        "nodes": {},
    }


def _watch(tmp_path: Path) -> Path:
    d = tmp_path / "inbox"
    d.mkdir()
    return d


def _claim_with_lease(
    watch: Path,
    name: str,
    *,
    child_pid: int | None,
    boot: str,
    claimed_at: float,
    ttl_s: int = 60,
    run_dir: Path | None = None,
    write_run: bool = False,
) -> Path:
    item = watch / name
    item.write_text(json.dumps({"x": 1}), encoding="utf-8")
    claimed = claim(watch, item)
    assert claimed is not None
    write_lease(
        watch,
        claimed.name,
        drain_pid=111,
        child_pid=child_pid,
        boot_id=boot,
        claimed_at=claimed_at,
        ttl_s=ttl_s,
    )
    if run_dir is not None:
        if write_run:
            create_run(run_dir.parent, run_dir.name, _payload(run_dir.name))
        write_pointer(
            pointer_path(claimed.parent, claimed.name),
            run_dir=run_dir,
            child_pid=child_pid,
        )
    return claimed


def _dead_kill(pid: int, sig: int) -> None:
    raise ProcessLookupError(errno.ESRCH, "No such process")


def _alive_kill(pid: int, sig: int) -> None:
    return None


def _eperm_kill(pid: int, sig: int) -> None:
    raise PermissionError(errno.EPERM, "Operation not permitted")


# --------------------------------------------------------------------------- #
# boot_id / pid_alive
# --------------------------------------------------------------------------- #


def test_pid_alive_none_and_zero_are_dead():
    assert pid_alive(None) is False
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


def test_pid_alive_uses_injected_kill_seam():
    assert pid_alive(4242, kill=_alive_kill) is True
    assert pid_alive(4242, kill=_dead_kill) is False
    assert pid_alive(4242, kill=_eperm_kill) is True  # EPERM = alive, not ours


def test_pid_alive_known_live_and_dead_pids():
    # This process is alive; a very high unused pid is almost certainly dead.
    assert pid_alive(os.getpid()) is True
    # PID 1 may or may not be killable; use injected dead seam for determinism above.
    # Negative check via ESRCH seam already covers the dead path.


def test_boot_id_caches_and_reset():
    boot_id(_reset=True)
    a = boot_id()
    b = boot_id()
    assert a == b
    assert isinstance(a, str) and len(a) > 0
    # On macOS we get kern.boottime; on Linux /proc boot_id; never empty unknown unless both fail.
    boot_id(_reset=True)


def test_boot_id_via_injected_runner():
    class FakeRunner:
        def run(self, argv, **kw):
            from cairn.kernel.proc import RunResult

            return RunResult(returncode=0, stdout="{ sec = 1, usec = 2 } fake-boot\n")

    boot_id(_reset=True)
    # Force sysctl path by making linux path miss: only works if /proc boot_id absent.
    # On Darwin: sysctl is used; inject runner.
    if Path("/proc/sys/kernel/random/boot_id").is_file():
        boot_id(_reset=True)
        pytest.skip("linux boot_id short-circuits before runner")
    got = boot_id(runner=FakeRunner(), _reset=True)
    assert "fake-boot" in got or "sec" in got
    boot_id(_reset=True)


def test_child_is_dead_different_boot_id():
    lease = {"child_pid": 99999, "drain_pid": 1, "boot_id": "boot-A"}
    assert child_is_dead(lease, current_boot_id="boot-B", kill=_alive_kill) is True


def test_child_is_dead_same_boot_consults_pid():
    lease = {"child_pid": 42, "drain_pid": 1, "boot_id": "boot-A"}
    assert child_is_dead(lease, current_boot_id="boot-A", kill=_alive_kill) is False
    assert child_is_dead(lease, current_boot_id="boot-A", kill=_dead_kill) is True


def test_child_is_dead_none_child_uses_drain_pid():
    """C1: child_pid=None is the claim→spawn window — consult drain_pid, not 'dead'."""
    live_drain = {"child_pid": None, "drain_pid": 42, "boot_id": "boot-A"}
    assert child_is_dead(live_drain, current_boot_id="boot-A", kill=_alive_kill) is False
    dead_drain = {"child_pid": None, "drain_pid": 42, "boot_id": "boot-A"}
    assert child_is_dead(dead_drain, current_boot_id="boot-A", kill=_dead_kill) is True


# --------------------------------------------------------------------------- #
# effective_lease_ttl policy
# --------------------------------------------------------------------------- #


def test_effective_lease_ttl_default_serial_off():
    assert effective_lease_ttl(LEASE_TTL_DEFAULT, concurrency=1) is None


def test_effective_lease_ttl_default_concurrency_on():
    assert effective_lease_ttl(LEASE_TTL_DEFAULT, concurrency=2) == DEFAULT_LEASE_TTL_S


def test_effective_lease_ttl_off_and_explicit():
    assert effective_lease_ttl(LEASE_TTL_OFF, concurrency=8) is None
    assert effective_lease_ttl(120, concurrency=1) == 120


# --------------------------------------------------------------------------- #
# Lease write / update
# --------------------------------------------------------------------------- #


def test_write_lease_content(tmp_path):
    watch = _watch(tmp_path)
    item = watch / "one.json"
    item.write_text("x", encoding="utf-8")
    claimed = claim(watch, item)
    assert claimed is not None
    write_lease(
        watch,
        claimed.name,
        drain_pid=7,
        child_pid=None,
        boot_id="boot-1",
        claimed_at=1000.0,
        ttl_s=3600,
    )
    path = lease_path(watch, claimed.name)
    assert path.is_file()
    # Dot-dir under .claim — not a top-level claim file.
    assert path.parent.name == ".leases"
    rec = read_lease(path)
    assert rec == {
        "drain_pid": 7,
        "child_pid": None,
        "boot_id": "boot-1",
        "claimed_at": 1000.0,
        "ttl_s": 3600,
    }


def test_update_lease_child_pid_after_spawn(tmp_path):
    watch = _watch(tmp_path)
    item = watch / "one.json"
    item.write_text("x", encoding="utf-8")
    claimed = claim(watch, item)
    write_lease(
        watch,
        claimed.name,
        drain_pid=1,
        child_pid=None,
        boot_id="b",
        claimed_at=1.0,
        ttl_s=60,
    )
    update_lease_child_pid(watch, claimed.name, 4242)
    rec = read_lease(lease_path(watch, claimed.name))
    assert rec["child_pid"] == 4242
    assert rec["drain_pid"] == 1


# --------------------------------------------------------------------------- #
# Reap decision table
# --------------------------------------------------------------------------- #


def test_reap_expired_dead_with_run_json_to_waiting(tmp_path):
    watch = _watch(tmp_path)
    run_dir = tmp_path / "runs" / "t-one"
    claimed = _claim_with_lease(
        watch,
        "one.json",
        child_pid=99,
        boot="boot-1",
        claimed_at=NOW_TS - 120,
        ttl_s=60,
        run_dir=run_dir,
        write_run=True,
    )
    reaped, flagged, diags = reap_expired_leases(
        watch,
        on_done="done",
        now=NOW_TS,
        current_boot_id="boot-1",
        kill=_dead_kill,
    )
    assert any(p.name == claimed.name for p in reaped)
    assert not flagged
    assert (watch / ".waiting" / "one.json").is_file()
    assert not (watch / ".claim" / "one.json").exists()
    assert not lease_path(watch, "one.json").exists()
    assert any("waiting" in d for d in diags)


def test_reap_expired_dead_no_run_json_to_inbox(tmp_path):
    watch = _watch(tmp_path)
    claimed = _claim_with_lease(
        watch,
        "husky.json",
        child_pid=99,
        boot="boot-1",
        claimed_at=NOW_TS - 120,
        ttl_s=60,
    )
    reaped, flagged, diags = reap_expired_leases(
        watch,
        on_done="done",
        now=NOW_TS,
        current_boot_id="boot-1",
        kill=_dead_kill,
    )
    assert any(p.name == claimed.name for p in reaped)
    assert not flagged
    assert (watch / "husky.json").is_file()
    assert not (watch / ".claim" / "husky.json").exists()
    assert any("inbox" in d for d in diags)


def test_reap_expired_alive_flagged_not_reaped(tmp_path):
    watch = _watch(tmp_path)
    _claim_with_lease(
        watch,
        "live.json",
        child_pid=99,
        boot="boot-1",
        claimed_at=NOW_TS - 120,
        ttl_s=60,
    )
    reaped, flagged, diags = reap_expired_leases(
        watch,
        on_done="done",
        now=NOW_TS,
        current_boot_id="boot-1",
        kill=_alive_kill,
    )
    assert not reaped
    assert any(p.name == "live.json" for p in flagged)
    assert (watch / ".claim" / "live.json").is_file()
    assert any("flagged" in d for d in diags)


def test_reap_not_expired_left(tmp_path):
    watch = _watch(tmp_path)
    _claim_with_lease(
        watch,
        "fresh.json",
        child_pid=99,
        boot="boot-1",
        claimed_at=NOW_TS - 10,
        ttl_s=60,
    )
    reaped, flagged, diags = reap_expired_leases(
        watch,
        on_done="done",
        now=NOW_TS,
        current_boot_id="boot-1",
        kill=_dead_kill,
    )
    assert not reaped
    assert not flagged
    assert (watch / ".claim" / "fresh.json").is_file()


def test_reap_different_boot_id_treated_as_dead(tmp_path):
    watch = _watch(tmp_path)
    _claim_with_lease(
        watch,
        "oldboot.json",
        child_pid=99,
        boot="boot-OLD",
        claimed_at=NOW_TS - 120,
        ttl_s=60,
    )
    # kill says alive, but boot differs → still reaped.
    reaped, flagged, _ = reap_expired_leases(
        watch,
        on_done="done",
        now=NOW_TS,
        current_boot_id="boot-NEW",
        kill=_alive_kill,
    )
    assert any(p.name == "oldboot.json" for p in reaped)
    assert not flagged
    assert (watch / "oldboot.json").is_file()  # no run.json → inbox


def test_missing_lease_under_lease_enabled_stuck_not_reaped(tmp_path):
    watch = _watch(tmp_path)
    item = watch / "nolease.json"
    item.write_text("x", encoding="utf-8")
    claimed = claim(watch, item)
    assert claimed is not None
    # No lease file written.
    reaped, flagged, diags = reap_expired_leases(
        watch,
        on_done="done",
        now=NOW_TS,
        current_boot_id="boot-1",
        kill=_dead_kill,
    )
    assert not reaped
    assert not flagged
    assert (watch / ".claim" / "nolease.json").is_file()
    assert any("missing lease" in d for d in diags)


def test_reap_expired_none_child_live_drain_not_reaped(tmp_path):
    """C1 race: expired lease, child_pid=None, LIVE drain_pid → flag, never reap.

    Models a concurrent reconcile sweep during the claim→spawn window while the
    original drain is still minting. Must leave the claim (and any reservation)
    intact so the live drain can finish spawn.
    """
    watch = _watch(tmp_path)
    item = watch / "p1-github-99-r1.json"
    item.write_text(
        json.dumps({"source": "github", "id": "99", "rev": "1", "prio": 1}),
        encoding="utf-8",
    )
    claimed = claim(watch, item)
    assert claimed is not None
    assert reserve_identity(watch, "github-99")
    live_drain = os.getpid()  # known-live
    write_lease(
        watch,
        claimed.name,
        drain_pid=live_drain,
        child_pid=None,  # not yet spawned
        boot_id="boot-1",
        claimed_at=NOW_TS - 120,
        ttl_s=60,
    )
    def kill_only_live_drain(pid: int, sig: int) -> None:
        if pid == live_drain:
            return  # alive
        raise ProcessLookupError(errno.ESRCH, "No such process")

    reaped, flagged, diags = reap_expired_leases(
        watch,
        on_done="done",
        now=NOW_TS,
        current_boot_id="boot-1",
        kill=kill_only_live_drain,
    )
    assert not reaped
    assert any(p.name == claimed.name for p in flagged)
    assert (watch / ".claim" / claimed.name).is_file()
    assert (watch / ".claim" / ".ids" / "github-99").is_file()  # reservation retained
    assert any("claim→spawn" in d or "not yet spawned" in d for d in diags)


def test_reap_expired_none_child_dead_drain_to_inbox(tmp_path):
    """C1 legitimate orphan: child_pid=None + DEAD drain_pid → reap to inbox.

    Drain crashed between claim and spawn — no validated run.json, release
    reservation, return item to inbox (T4 redelivery).
    """
    watch = _watch(tmp_path)
    item = watch / "p1-github-88-r1.json"
    item.write_text(
        json.dumps({"source": "github", "id": "88", "rev": "1", "prio": 1}),
        encoding="utf-8",
    )
    claimed = claim(watch, item)
    assert claimed is not None
    assert reserve_identity(watch, "github-88")
    dead_drain = 999_999_999  # known-dead via kill seam
    write_lease(
        watch,
        claimed.name,
        drain_pid=dead_drain,
        child_pid=None,
        boot_id="boot-1",
        claimed_at=NOW_TS - 120,
        ttl_s=60,
    )
    reaped, flagged, diags = reap_expired_leases(
        watch,
        on_done="done",
        now=NOW_TS,
        current_boot_id="boot-1",
        kill=_dead_kill,
    )
    assert any(p.name == claimed.name for p in reaped)
    assert not flagged
    assert (watch / claimed.name).is_file()  # back in inbox
    assert not (watch / ".claim" / claimed.name).exists()
    assert not (watch / ".claim" / ".ids" / "github-88").exists()  # reservation released
    assert any("inbox" in d for d in diags)


def test_sweep_passes_lease_ttl_and_reaps(tmp_path):
    watch = _watch(tmp_path)
    _claim_with_lease(
        watch,
        "s.json",
        child_pid=1,
        boot="b",
        claimed_at=NOW_TS - 999,
        ttl_s=10,
    )
    report = sweep(
        watch,
        on_done="done",
        now=NOW_TS,
        lease_ttl_s=10,
        current_boot_id="b",
        kill=_dead_kill,
    )
    assert any(p.name == "s.json" for p in report.reaped)
    assert (watch / "s.json").is_file()


def test_sweep_without_lease_ttl_does_not_reap(tmp_path):
    """Serial / lease-off path: sweep does not touch stuck claims."""
    watch = _watch(tmp_path)
    _claim_with_lease(
        watch,
        "s.json",
        child_pid=1,
        boot="b",
        claimed_at=NOW_TS - 999,
        ttl_s=10,
    )
    report = sweep(watch, on_done="done", now=NOW_TS)  # lease_ttl_s=None
    assert not report.reaped
    assert (watch / ".claim" / "s.json").is_file()


# --------------------------------------------------------------------------- #
# D7: default serial trigger — no lease, stuck-forever
# --------------------------------------------------------------------------- #


def test_default_serial_trigger_writes_no_lease(tmp_path, monkeypatch):
    """concurrency default 1, no lease key → no lease file on claim."""
    import cairn.kernel.queue_drain as qd
    from cairn.kernel.proc import RunnerBase, RunResult
    from cairn.kernel.runctl import Minted

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "cairn.toml").write_text("[workspace]\nname = \"t\"\n", encoding="utf-8")
    (ws / "pipelines").mkdir()
    (ws / "pipelines" / "p.yaml").write_text(
        "pipeline: p\nparams: {}\nsteps:\n  - id: a\n    run: echo hi\n",
        encoding="utf-8",
    )
    (ws / "triggers.yaml").write_text(
        "t1:\n  pipeline: p\n  watch: inbox/\n",
        encoding="utf-8",
    )
    inbox = ws / "inbox"
    inbox.mkdir()
    (inbox / "one.json").write_text("payload", encoding="utf-8")

    triggers = load_triggers(ws)
    assert triggers["t1"].concurrency == 1
    assert triggers["t1"].lease_ttl_s == LEASE_TTL_DEFAULT
    assert effective_lease_ttl(triggers["t1"].lease_ttl_s, 1) is None

    run_dir = ws / "runs" / "t1-one"
    create_run(ws / "runs", "t1-one", _payload("t1-one"))

    def mint(*a, **k):
        return Minted(run_dir=run_dir)

    class H:
        pid = 5555

        def wait(self, timeout=None):
            return RunResult(returncode=0, stdout="", stderr="")

        def poll(self):
            return 0

        def terminate(self):
            return None

    class R(RunnerBase):
        def spawn(self, argv, *, input=None, cwd=None):
            return H()

    monkeypatch.setattr(qd, "preallocate_run", mint)
    err = io.StringIO()
    code = run_trigger("t1", ws, runner=R(), cairn_bin="cairn", now=NOW, err=err)
    assert code == 0
    # Done path: item retired; no lease residue under .claim/.leases
    leases = inbox / ".claim" / ".leases"
    if leases.is_dir():
        assert list(leases.iterdir()) == []
    # During claim there was no lease — prove via a mid-claim stuck scenario below.


def test_default_serial_stuck_claim_never_auto_reaped(tmp_path):
    """D7: stuck claim without lease key stays forever under sweep(lease_ttl=None)."""
    watch = _watch(tmp_path)
    item = watch / "stuck.json"
    item.write_text("x", encoding="utf-8")
    claimed = claim(watch, item)
    assert claimed is not None
    # No lease written (serial path).
    report = sweep(watch, on_done="done", now=NOW_TS)
    assert not report.reaped
    assert claimed.is_file()


def test_concurrency_gt1_default_enables_lease_on_claim(tmp_path, monkeypatch):
    import cairn.kernel.queue_drain as qd
    from cairn.kernel.proc import RunnerBase, RunResult
    from cairn.kernel.runctl import Minted

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "cairn.toml").write_text("[workspace]\nname = \"t\"\n", encoding="utf-8")
    (ws / "pipelines").mkdir()
    (ws / "pipelines" / "p.yaml").write_text(
        "pipeline: p\nparams: {}\nsteps:\n  - id: a\n    run: echo hi\n",
        encoding="utf-8",
    )
    (ws / "triggers.yaml").write_text(
        "t1:\n  pipeline: p\n  watch: inbox/\n  concurrency: 2\n",
        encoding="utf-8",
    )
    inbox = ws / "inbox"
    inbox.mkdir()
    (inbox / "one.json").write_text("payload", encoding="utf-8")

    triggers = load_triggers(ws)
    assert effective_lease_ttl(triggers["t1"].lease_ttl_s, 2) == DEFAULT_LEASE_TTL_S

    seen: list[dict] = []
    child_updates: list[tuple[str, int]] = []
    real_write = qd.write_lease
    real_update = qd.update_lease_child_pid

    def tracking_write(watch_abs, item_name, **kw):
        seen.append({"name": item_name, **kw})
        return real_write(watch_abs, item_name, **kw)

    def tracking_update(watch_abs, item_name, child_pid, **kw):
        child_updates.append((item_name, child_pid))
        return real_update(watch_abs, item_name, child_pid, **kw)

    monkeypatch.setattr(qd, "write_lease", tracking_write)
    monkeypatch.setattr(qd, "update_lease_child_pid", tracking_update)

    run_dir = ws / "runs" / "t1-one"
    create_run(ws / "runs", "t1-one", _payload("t1-one"))

    def mint(*a, **k):
        return Minted(run_dir=run_dir)

    class H:
        pid = 7777

        def wait(self, timeout=None):
            return RunResult(returncode=0, stdout="", stderr="")

        def poll(self):
            return 0

        def terminate(self):
            return None

    class R(RunnerBase):
        def spawn(self, argv, *, input=None, cwd=None):
            return H()

    monkeypatch.setattr(qd, "preallocate_run", mint)
    code = run_trigger("t1", ws, runner=R(), cairn_bin="cairn", now=NOW)
    assert code == 0
    assert seen, "lease must be written on claim when concurrency>1"
    assert seen[0]["child_pid"] is None  # claim-time
    assert seen[0]["ttl_s"] == DEFAULT_LEASE_TTL_S
    # child_pid filled after spawn (update_lease_child_pid, mirrors pointer).
    assert ("one.json", 7777) in child_updates


def test_load_trigger_lease_off_and_duration(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "pipelines").mkdir()
    (ws / "pipelines" / "p.yaml").write_text(
        "pipeline: p\nparams: {}\nsteps:\n  - id: a\n    run: echo hi\n",
        encoding="utf-8",
    )
    (ws / "triggers.yaml").write_text(
        "a:\n  pipeline: p\n  watch: inbox/\n  lease: off\n"
        "b:\n  pipeline: p\n  watch: inbox2/\n  lease: 30m\n"
        "c:\n  pipeline: p\n  watch: inbox3/\n  concurrency: 3\n  lease: off\n",
        encoding="utf-8",
    )
    triggers = load_triggers(ws)
    assert triggers["a"].lease_ttl_s == LEASE_TTL_OFF
    assert effective_lease_ttl(triggers["a"].lease_ttl_s, 1) is None
    assert triggers["b"].lease_ttl_s == 1800
    assert effective_lease_ttl(triggers["b"].lease_ttl_s, 1) == 1800
    assert effective_lease_ttl(triggers["c"].lease_ttl_s, 3) is None


def test_load_trigger_lease_garbage_raises(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "pipelines").mkdir()
    (ws / "pipelines" / "p.yaml").write_text(
        "pipeline: p\nparams: {}\nsteps:\n  - id: a\n    run: echo hi\n",
        encoding="utf-8",
    )
    (ws / "triggers.yaml").write_text(
        "a:\n  pipeline: p\n  watch: inbox/\n  lease: banana\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="lease"):
        load_triggers(ws)


# --------------------------------------------------------------------------- #
# Stranded deferred mop (T12 residual)
# --------------------------------------------------------------------------- #


def test_mop_stranded_deferred_promotes_free_identity(tmp_path):
    watch = _watch(tmp_path)
    deferred = watch / ".deferred"
    deferred.mkdir()
    body = {
        "source": "github",
        "id": "42",
        "rev": "20",
        "prio": 1,
    }
    (deferred / "github-42").write_text(json.dumps(body), encoding="utf-8")
    # Orphan reservation (no live claim/waiting).
    assert reserve_identity(watch, "github-42")
    diags = mop_stranded_deferred(watch)
    assert any("promoted stranded" in d for d in diags)
    assert (watch / "p1-github-42-r20.json").is_file()
    assert not (deferred / "github-42").exists()
    assert not (watch / ".claim" / ".ids" / "github-42").exists()


def test_mop_stranded_deferred_leaves_live_identity(tmp_path):
    watch = _watch(tmp_path)
    deferred = watch / ".deferred"
    deferred.mkdir()
    body = {"source": "github", "id": "7", "rev": "2", "prio": 1}
    (deferred / "github-7").write_text(json.dumps(body), encoding="utf-8")
    # Live claim for same identity.
    live = watch / "p1-github-7-r1.json"
    live.write_text(json.dumps({"source": "github", "id": "7", "rev": "1", "prio": 1}))
    claimed = claim(watch, live)
    assert claimed is not None
    diags = mop_stranded_deferred(watch)
    assert not any("promoted" in d for d in diags)
    assert (deferred / "github-7").is_file()


# --------------------------------------------------------------------------- #
# factory reconcile
# --------------------------------------------------------------------------- #


def _workspace_two_triggers(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "cairn.toml").write_text("[workspace]\nname = \"t\"\n", encoding="utf-8")
    (ws / "pipelines").mkdir()
    (ws / "pipelines" / "p.yaml").write_text(
        "pipeline: p\nparams: {}\nsteps:\n  - id: a\n    run: echo hi\n",
        encoding="utf-8",
    )
    (ws / "triggers.yaml").write_text(
        "alpha:\n  pipeline: p\n  watch: inbox/a/\n  concurrency: 2\n"
        "beta:\n  pipeline: p\n  watch: inbox/b/\n  lease: 1m\n",
        encoding="utf-8",
    )
    (ws / "inbox" / "a").mkdir(parents=True)
    (ws / "inbox" / "b").mkdir(parents=True)
    return ws


def test_reconcile_sweeps_multiple_triggers_reaps_and_promotes(tmp_path):
    ws = _workspace_two_triggers(tmp_path)
    watch_a = ws / "inbox" / "a"
    watch_b = ws / "inbox" / "b"

    # Expired dead claim on alpha (lease-enabled via concurrency>1).
    _claim_with_lease(
        watch_a,
        "stuck.json",
        child_pid=1,
        boot="boot-1",
        claimed_at=NOW_TS - 9999,
        ttl_s=60,
    )

    # Stranded deferred on beta.
    deferred = watch_b / ".deferred"
    deferred.mkdir()
    body = {"source": "github", "id": "9", "rev": "3", "prio": 1}
    (deferred / "github-9").write_text(json.dumps(body), encoding="utf-8")

    out = io.StringIO()
    err = io.StringIO()
    # Patch kill via sweep's kill param — reconcile doesn't expose kill.
    # Plant a dead child by using child_pid that os.kill will ESRCH, or
    # use boot mismatch so reap doesn't need kill.
    # Re-write alpha lease with different boot so reap is deterministic.
    write_lease(
        watch_a,
        "stuck.json",
        drain_pid=1,
        child_pid=1,
        boot_id="boot-OLD",
        claimed_at=NOW_TS - 9999,
        ttl_s=60,
    )

    report = reconcile_workspace(ws, now=NOW, out=out, err=err)
    assert not report.already_running
    assert not report.hazarded
    names = {s.name for s in report.summaries}
    assert names == {"alpha", "beta"}
    alpha = next(s for s in report.summaries if s.name == "alpha")
    beta = next(s for s in report.summaries if s.name == "beta")
    assert alpha.reaped >= 1
    assert beta.promoted_deferred >= 1
    assert (watch_a / "stuck.json").is_file()  # reaped to inbox
    assert (watch_b / "p1-github-9-r3.json").is_file()
    assert "reconcile alpha" in out.getvalue()
    assert "reconcile beta" in out.getvalue()


def test_reconcile_single_flight(tmp_path):
    ws = _workspace_two_triggers(tmp_path)
    held = threading.Event()
    contender_done = threading.Event()
    results: list = []

    def holder():
        from cairn.kernel.queue_drain import _reconcile_lock

        with _reconcile_lock(ws) as got:
            assert got is True
            held.set()
            # Stay held until contender finishes.
            contender_done.wait(timeout=5)

    def contender():
        held.wait(timeout=5)
        out = io.StringIO()
        report = reconcile_workspace(ws, now=NOW, out=out)
        results.append((report, out.getvalue()))
        contender_done.set()

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=contender)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert results
    report, text = results[0]
    assert report.already_running is True
    assert "already running" in text
