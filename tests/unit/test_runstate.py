"""run.json lifecycle + advisory run locking.

Behaviour tests against the public surface (create_run / load_run / update_run,
node_status / set_node_status, run_lock) — API.md §8.1 (run.json schema) and
SECURITY.md §5 (run locking).
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os

import pytest

from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.runstate import (
    LockHeldError,
    RunExistsError,
    create_run,
    load_run,
    node_status,
    run_lock,
    set_node_status,
    update_run,
)


def _payload(run_id: str = "acme-redesign-20260703") -> dict:
    """A minimal run.json that satisfies cairn:run."""
    return {
        "run_id": run_id,
        "pipeline": "brease-rebuild",
        "pipeline_hash": "sha256:abc",
        "cairn_version": "0.1.0",
        "params": {"url": "https://acme.test", "mode": "redesign"},
        "dims": {"content": "keep", "design": "redesign", "brand": "keep", "routes": "keep"},
        "executors": {"default": "codex"},
        "models": {"capture": "gpt-5.4/medium"},
        "created_at": "2026-07-03T10:00:00.000Z",
        "status": "running",
        "nodes": {},
    }


def test_create_run_writes_a_validated_run_json(tmp_path):
    run_dir = create_run(tmp_path, "acme-redesign-20260703", _payload())
    assert run_dir == tmp_path / "acme-redesign-20260703"
    on_disk = json.loads((run_dir / "run.json").read_text())
    assert on_disk["run_id"] == "acme-redesign-20260703"
    assert on_disk["status"] == "running"


def test_create_run_rejects_a_collision(tmp_path):
    create_run(tmp_path, "acme-redesign-20260703", _payload())
    with pytest.raises(RunExistsError):
        create_run(tmp_path, "acme-redesign-20260703", _payload())
    assert issubclass(RunExistsError, CairnError)


def test_create_run_rejects_an_invalid_payload(tmp_path):
    bad = _payload()
    del bad["status"]  # required by the schema
    with pytest.raises(ConfigError):
        create_run(tmp_path, "acme-redesign-20260703", bad)
    # Nothing half-written is left behind.
    assert not (tmp_path / "acme-redesign-20260703" / "run.json").exists()


def test_load_run_returns_the_validated_dict(tmp_path):
    create_run(tmp_path, "acme-redesign-20260703", _payload())
    loaded = load_run(tmp_path / "acme-redesign-20260703")
    assert loaded["pipeline"] == "brease-rebuild"


def test_load_run_rejects_a_corrupted_run_json(tmp_path):
    run_dir = create_run(tmp_path, "acme-redesign-20260703", _payload())
    doc = json.loads((run_dir / "run.json").read_text())
    doc["status"] = "on-fire"  # not in the enum
    (run_dir / "run.json").write_text(json.dumps(doc))
    with pytest.raises(ConfigError):
        load_run(run_dir)


def test_update_run_atomically_mutates_and_revalidates(tmp_path):
    run_dir = create_run(tmp_path, "acme-redesign-20260703", _payload())

    def mark_done(doc: dict) -> None:
        doc["status"] = "done"

    result = update_run(run_dir, mark_done)
    assert result["status"] == "done"
    assert load_run(run_dir)["status"] == "done"


def test_update_run_rejecting_an_invalid_mutation_leaves_the_file_intact(tmp_path):
    run_dir = create_run(tmp_path, "acme-redesign-20260703", _payload())

    def corrupt(doc: dict) -> None:
        doc["status"] = "melted"

    with pytest.raises(ConfigError):
        update_run(run_dir, corrupt)
    assert load_run(run_dir)["status"] == "running"  # untouched on disk


def test_node_status_helpers_round_trip(tmp_path):
    run_dir = create_run(tmp_path, "acme-redesign-20260703", _payload())

    def advance(doc: dict) -> None:
        set_node_status(doc, "capture", "done", at="2026-07-03T10:05:00.000Z", cycles=2)

    doc = update_run(run_dir, advance)
    assert node_status(doc, "capture") == "done"
    assert doc["nodes"]["capture"]["at"] == "2026-07-03T10:05:00.000Z"
    assert doc["nodes"]["capture"]["cycles"] == 2
    # An unknown node has no status.
    assert node_status(doc, "nope") is None


def test_atomic_write_fsyncs_the_tmp_file_before_replacing(tmp_path, monkeypatch):
    # run.json is a state authority; like the trail it must be durable, not just atomic,
    # so a power loss between write and rename can't leave an empty/truncated manifest.
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd):
        order.append("fsync")
        return real_fsync(fd)

    def spy_replace(src, dst):
        order.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)

    create_run(tmp_path, "acme-redesign-20260703", _payload())

    assert "fsync" in order, "tmp file must be fsynced before the rename"
    assert order.index("fsync") < order.index("replace")


def test_run_lock_is_exclusive_and_reports_the_holder_pid(tmp_path):
    run_dir = create_run(tmp_path, "acme-redesign-20260703", _payload())
    with run_lock(run_dir):
        # A second acquisition of the same run's lock must fail loudly with the holder PID.
        with pytest.raises(LockHeldError) as exc:
            with run_lock(run_dir):
                pass
        assert exc.value.pid is not None
    assert issubclass(LockHeldError, CairnError)


def test_run_lock_is_released_on_exit(tmp_path):
    run_dir = create_run(tmp_path, "acme-redesign-20260703", _payload())
    with run_lock(run_dir):
        pass
    # Re-acquirable once released — no leaked lock.
    with run_lock(run_dir):
        pass


def _hold_lock(run_dir, ready, release):
    with run_lock(run_dir):
        ready.set()
        release.wait(timeout=5)


def test_run_lock_excludes_a_separate_process(tmp_path):
    run_dir = create_run(tmp_path, "acme-redesign-20260703", _payload())
    ready, release = mp.Event(), mp.Event()
    proc = mp.Process(target=_hold_lock, args=(run_dir, ready, release))
    proc.start()
    try:
        assert ready.wait(timeout=5)
        with pytest.raises(LockHeldError) as exc:
            with run_lock(run_dir):
                pass
        assert exc.value.pid == proc.pid
    finally:
        release.set()
        proc.join(timeout=5)
