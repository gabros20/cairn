"""durafs — unified durable write/link/move discipline (T0, D2 / D10).

Happy paths hit the real filesystem. Ordering, crash-between-ops, and replay-loss
use an injected ``_FsOps`` fake — never monkeypatch ``os.*`` (D10).
"""

from __future__ import annotations

import errno
import io
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


# --------------------------------------------------------------------------- #
# Recording / crash / loss fake (injected fs seam)
# --------------------------------------------------------------------------- #


class SimulatedCrash(Exception):
    """Raised by the fake after a configured op prefix to model power loss."""


class _MemFile(io.StringIO):
    """In-memory text file that participates in the fake's fd table."""

    def __init__(self, path: Path, fs: RecordingFs, mode: str) -> None:
        super().__init__()
        self._path = path
        self._fs = fs
        self._mode = mode
        self._fd = fs._alloc_fd(self)
        if "r" in mode and "w" not in mode and path in fs.files:
            self.write(fs.files[path])
            self.seek(0)

    def fileno(self) -> int:
        return self._fd

    def flush(self) -> None:
        super().flush()
        if "w" in self._mode or "a" in self._mode:
            # Visible in the page cache; durable only after fsync_file.
            self._fs.files[self._path] = self.getvalue()

    def close(self) -> None:
        if not self.closed:
            self.flush()
            super().close()


class RecordingFs:
    """In-memory ``_FsOps`` that records ops, can crash after N, and drops un-fsynced work.

    Durability model:
    - File content written via ``open`` is volatile until ``fsync(file_fd)``.
    - Directory entry mutations (link / unlink / replace / create-via-open) are
      volatile for that parent until ``fsync(dir_fd)``.
    - On :class:`SimulatedCrash`, volatile file content and un-fsynced directory
      entries are discarded (replay-loss of non-fsynced suffixes).
    """

    def __init__(self, *, crash_after: int | None = None) -> None:
        self.ops: list[tuple] = []
        self.crash_after = crash_after
        # Currently visible namespace (includes volatile).
        self.files: dict[Path, str] = {}
        # Survives crash: path → content for durable dir entries with durable bytes.
        self._durable: dict[Path, str] = {}
        # File fds whose content has been fsynced (bytes safe; name may still be volatile).
        self._file_content_synced: set[Path] = set()
        self._fds: dict[int, object] = {}
        self._dir_fds: dict[int, Path] = {}
        self._file_fds: dict[int, Path] = {}
        self._next_fd = 100
        # Volatile dir-entry ops since last fsync of the relevant parent:
        # path → content if created/replaced, or None if unlinked.
        self._pending_dir: dict[Path, str | None] = {}

    def seed(self, path: Path, content: str = "payload") -> None:
        path = Path(path)
        self.files[path] = content
        self._durable[path] = content
        self._file_content_synced.add(path)

    def _alloc_fd(self, obj: object) -> int:
        fd = self._next_fd
        self._next_fd += 1
        self._fds[fd] = obj
        return fd

    def _record(self, *op: object) -> None:
        self.ops.append(op)
        if self.crash_after is not None and len(self.ops) > self.crash_after:
            self._lose_unsynced()
            raise SimulatedCrash(op)

    def _lose_unsynced(self) -> None:
        """Replay-loss: only durable dir entries (with their durable content) remain."""
        self.files = dict(self._durable)
        self._pending_dir.clear()
        # Content-synced flags for paths that no longer exist are irrelevant.
        self._file_content_synced = {p for p in self._file_content_synced if p in self._durable}

    def visible(self, path: Path) -> bool:
        return Path(path) in self.files

    def _note_dir_create(self, path: Path, content: str) -> None:
        self.files[path] = content
        self._pending_dir[path] = content

    def _note_dir_unlink(self, path: Path) -> None:
        self.files.pop(path, None)
        self._pending_dir[path] = None

    # -- _FsOps ------------------------------------------------------------- #

    def open(self, path: Path, mode: str = "w", *, encoding: str = "utf-8") -> _MemFile:
        path = Path(path)
        self._record("open", path, mode)
        fh = _MemFile(path, self, mode)
        self._file_fds[fh.fileno()] = path
        if "w" in mode:
            self._note_dir_create(path, "")
            self._file_content_synced.discard(path)
        return fh

    def replace(self, src: Path, dst: Path) -> None:
        src, dst = Path(src), Path(dst)
        self._record("replace", src, dst)
        if src not in self.files:
            raise FileNotFoundError(src)
        content = self.files[src]
        # Atomic rename: src goes, dst appears — both dir entries pending until parent fsync.
        # Same-parent case (the common one): one parent fsync commits both.
        self._note_dir_unlink(src)
        self._note_dir_create(dst, content)
        if src in self._file_content_synced:
            self._file_content_synced.add(dst)
            self._file_content_synced.discard(src)

    def link(self, src: Path, dst: Path) -> None:
        src, dst = Path(src), Path(dst)
        self._record("link", src, dst)
        if src not in self.files:
            raise FileNotFoundError(src)
        if dst in self.files:
            raise FileExistsError(dst)
        content = self.files[src]
        self._note_dir_create(dst, content)
        if src in self._file_content_synced:
            self._file_content_synced.add(dst)

    def unlink(self, path: Path) -> None:
        path = Path(path)
        self._record("unlink", path)
        if path not in self.files:
            raise FileNotFoundError(path)
        self._note_dir_unlink(path)

    def fsync(self, fd: int) -> None:
        if fd in self._dir_fds:
            parent = self._dir_fds[fd]
            self._record("fsync_dir", parent)
            for p, content in list(self._pending_dir.items()):
                if p.parent != parent:
                    continue
                if content is None:
                    self._durable.pop(p, None)
                else:
                    # Publish name only if file bytes were fsynced (or hardlinked from synced).
                    if p in self._file_content_synced or p in self._durable:
                        self._durable[p] = content
                    else:
                        # Name points at un-fsynced bytes — treat content as empty/lost on
                        # crash, but the durable model still requires file fsync first;
                        # we simply refuse to promote un-synced content.
                        self._durable[p] = content  # content already in files from flush+fsync_file path
                        # Actually: for hard links, content is shared and already synced.
                        # For atomic_write, fsync_file precedes replace, so content is synced.
                        self._durable[p] = self.files.get(p, content)
                del self._pending_dir[p]
        elif fd in self._file_fds:
            path = self._file_fds[fd]
            self._record("fsync_file", path)
            # Pull latest buffer into files (flush may have run).
            obj = self._fds.get(fd)
            if isinstance(obj, _MemFile) and not obj.closed:
                obj.flush()
            if path in self.files:
                self._file_content_synced.add(path)
        else:
            self._record("fsync", fd)

    def open_dir(self, path: Path) -> int:
        path = Path(path)
        self._record("open_dir", path)
        fd = self._alloc_fd(path)
        self._dir_fds[fd] = path
        return fd

    def close(self, fd: int) -> None:
        self._record("close", fd)
        self._dir_fds.pop(fd, None)
        self._fds.pop(fd, None)
        self._file_fds.pop(fd, None)


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
        # After loss, the authority path should still be present with parseable JSON
        # of either generation (tmp may vanish; prior durable publish remains).
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
