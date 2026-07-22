"""durafs — unified durable write/link/move discipline (T0, D2 / D10).

Happy paths hit the real filesystem. Ordering, crash-between-ops, and replay-loss
use the shared :mod:`fstestkit` fake — never monkeypatch ``os.*`` (D10).
"""

from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest

from cairn.kernel.durafs import (
    atomic_write_json,
    atomic_write_text,
    durable_link,
    durable_move,
    durable_unlink,
    fsync_dir,
)
from fstestkit import RecordingFs, SimulatedCrash


# --------------------------------------------------------------------------- #
# Happy paths (real filesystem)
# --------------------------------------------------------------------------- #


def test_atomic_write_json_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    atomic_write_json(path, {"status": "running", "n": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "running", "n": 1}
    assert path.read_text(encoding="utf-8") == json.dumps(
        {"status": "running", "n": 1}, indent=2, ensure_ascii=False
    )


def test_atomic_write_text_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "pointer"
    atomic_write_text(path, "claims/abc\n")
    assert path.read_text(encoding="utf-8") == "claims/abc\n"


def test_fsync_dir_on_real_fs(tmp_path: Path) -> None:
    fsync_dir(tmp_path)


def test_durable_link_and_unlink(tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("hello", encoding="utf-8")
    durable_link(src, dest)
    assert dest.read_text(encoding="utf-8") == "hello"
    assert src.stat().st_ino == dest.stat().st_ino
    durable_unlink(src)
    assert not src.exists()
    assert dest.read_text(encoding="utf-8") == "hello"


def test_durable_move_leaves_only_dest(tmp_path: Path) -> None:
    src = tmp_path / "a" / "src.txt"
    dest = tmp_path / "b" / "dest.txt"
    src.parent.mkdir()
    dest.parent.mkdir()
    src.write_text("payload", encoding="utf-8")
    durable_move(src, dest)
    assert not src.exists()
    assert dest.read_text(encoding="utf-8") == "payload"


def test_durable_link_existing_dest_raises_file_exists(tmp_path: Path) -> None:
    """Collision case that gates QTP claim races: dest taken → FileExistsError, both intact."""
    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("from-src", encoding="utf-8")
    dest.write_text("already-here", encoding="utf-8")
    with pytest.raises(FileExistsError):
        durable_link(src, dest)
    assert src.read_text(encoding="utf-8") == "from-src"
    assert dest.read_text(encoding="utf-8") == "already-here"
    assert src.exists() and dest.exists()


def test_durable_move_existing_dest_raises_file_exists(tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("from-src", encoding="utf-8")
    dest.write_text("already-here", encoding="utf-8")
    with pytest.raises(FileExistsError):
        durable_move(src, dest)
    assert src.read_text(encoding="utf-8") == "from-src"
    assert dest.read_text(encoding="utf-8") == "already-here"


# --------------------------------------------------------------------------- #
# Ordering via injected fake
# --------------------------------------------------------------------------- #


def test_atomic_write_fsyncs_file_before_replace() -> None:
    fs = RecordingFs()
    path = Path("/run/run.json")
    atomic_write_json(path, {"ok": True}, fs=fs)

    kinds = [op[0] for op in fs.ops]
    assert "fsync_file" in kinds
    assert "replace" in kinds
    assert kinds.index("fsync_file") < kinds.index("replace")
    fsync_dirs = [i for i, op in enumerate(fs.ops) if op[0] == "fsync_dir"]
    assert fsync_dirs, "parent directory must be fsynced"
    assert kinds.index("replace") < fsync_dirs[0]


def test_durable_move_fsyncs_dest_parent_before_src_unlink() -> None:
    fs = RecordingFs()
    src = Path("/inbox/item.json")
    dest = Path("/claim/item.json")
    fs.seed(src, "body")

    durable_move(src, dest, fs=fs)

    kinds = [op[0] for op in fs.ops]
    assert kinds.count("link") == 1
    assert kinds.count("unlink") == 1
    fsync_dir_idxs = [i for i, k in enumerate(kinds) if k == "fsync_dir"]
    unlink_idx = kinds.index("unlink")
    link_idx = kinds.index("link")
    assert fsync_dir_idxs, "expected directory fsyncs"
    assert link_idx < fsync_dir_idxs[0] < unlink_idx
    assert fs.ops[fsync_dir_idxs[0]] == ("fsync_dir", dest.parent)
    assert not fs.visible(src)
    assert fs.visible(dest)


def test_durable_link_fsyncs_dest_parent() -> None:
    fs = RecordingFs()
    src = Path("/a/x")
    dest = Path("/b/y")
    fs.seed(src)
    durable_link(src, dest, fs=fs)
    assert ("fsync_dir", dest.parent) in fs.ops
    assert fs.visible(dest)


def test_durable_unlink_fsyncs_parent() -> None:
    fs = RecordingFs()
    path = Path("/a/x")
    fs.seed(path)
    durable_unlink(path, fs=fs)
    assert ("fsync_dir", path.parent) in fs.ops
    assert not fs.visible(path)


def test_recording_fs_refuses_promotion_without_file_fsync() -> None:
    """Prior durability of a path must not auto-promote unsynced rewrite bytes.

    Models the re-write case (run.json / ledger / lease): seed durable content,
    stage a pending dir entry with new bytes that were never file-fsynced, dir-fsync,
    then assert promotion is refused and crash recovery keeps the old content.
    """
    fs = RecordingFs()
    path = Path("/run/run.json")
    fs.seed(path, "old-durable")
    # Rewrite visible in the page cache but not content-fsynced this generation.
    fs.files[path] = "new-unsynced"
    fs._pending_dir[path] = "new-unsynced"
    fs._file_content_synced.discard(path)

    dir_fd = fs.open_dir(path.parent)
    fs.fsync(dir_fd)
    fs.close(dir_fd)

    assert fs._durable[path] == "old-durable"
    assert path not in fs._file_content_synced
    fs._lose_unsynced()
    assert fs.files[path] == "old-durable"


# --------------------------------------------------------------------------- #
# Replay-loss: crash after each op prefix → legal QTP visible state
# --------------------------------------------------------------------------- #


def test_durable_move_replay_loss_legal_qtp_states() -> None:
    """Crash after every op prefix; post-loss state is always a legal QTP shape.

    Legal shapes: src-only, both, dest-only. Never neither once the dest link
    has been directory-fsynced (and never neither from a seeded start).
    """
    src = Path("/inbox/item.json")
    dest = Path("/claim/item.json")

    probe = RecordingFs()
    probe.seed(src, "body")
    durable_move(src, dest, fs=probe)
    n_ops = len(probe.ops)
    assert n_ops > 0

    for crash_after in range(n_ops + 1):
        fs = RecordingFs(crash_after=crash_after if crash_after < n_ops else None)
        fs.seed(src, "body")
        try:
            durable_move(src, dest, fs=fs)
        except SimulatedCrash:
            pass
        src_present = fs.visible(src)
        dest_present = fs.visible(dest)
        assert src_present or dest_present, (
            f"illegal neither-state after crash_after={crash_after}, ops={fs.ops}"
        )
        assert (src_present, dest_present) in {
            (True, False),
            (True, True),
            (False, True),
        }


def test_atomic_write_replay_loss_no_corrupt_authority() -> None:
    """Crash mid-write: authority path keeps a prior durable doc or the new one — not neither
    after a successful prior write, and never a half-applied non-JSON body.
    """
    path = Path("/run/run.json")
    old = json.dumps({"v": 1}, indent=2, ensure_ascii=False)

    dry = RecordingFs()
    dry.seed(path, old)
    atomic_write_json(path, {"v": 2}, fs=dry)
    n_ops = len(dry.ops)

    for crash_after in range(n_ops + 1):
        fs = RecordingFs(crash_after=crash_after if crash_after < n_ops else None)
        fs.seed(path, old)
        try:
            atomic_write_json(path, {"v": 2}, fs=fs)
        except SimulatedCrash:
            pass
        assert fs.visible(path), f"authority missing after crash_after={crash_after}"
        doc = json.loads(fs.files[path])
        assert doc in ({"v": 1}, {"v": 2})


# --------------------------------------------------------------------------- #
# EXDEV propagates untouched
# --------------------------------------------------------------------------- #


def test_durable_move_propagates_exdev() -> None:
    class ExdevFs(RecordingFs):
        def link(self, src: Path, dst: Path) -> None:
            self._record("link", Path(src), Path(dst))
            raise OSError(errno.EXDEV, "Invalid cross-device link")

    fs = ExdevFs()
    src = Path("/mnt/a/x")
    dest = Path("/mnt/b/x")
    fs.seed(src)
    with pytest.raises(OSError) as excinfo:
        durable_move(src, dest, fs=fs)
    assert excinfo.value.errno == errno.EXDEV
    assert fs.visible(src)
    assert not fs.visible(dest)


def test_durable_link_propagates_exdev() -> None:
    class ExdevFs(RecordingFs):
        def link(self, src: Path, dst: Path) -> None:
            self._record("link", Path(src), Path(dst))
            raise OSError(errno.EXDEV, "Invalid cross-device link")

    fs = ExdevFs()
    src = Path("/mnt/a/x")
    dest = Path("/mnt/b/x")
    fs.seed(src)
    with pytest.raises(OSError) as excinfo:
        durable_link(src, dest, fs=fs)
    assert excinfo.value.errno == errno.EXDEV
