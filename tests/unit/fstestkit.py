"""Shared filesystem fakes for durability / QTP crash-prefix tests (durafs, ledger, leases).

Inject these via durafs' keyword-only ``fs=`` seam — never monkeypatch ``os.*`` (D10).
"""

from __future__ import annotations

import io
from pathlib import Path


class SimulatedCrash(Exception):
    """Raised by the fake after a configured op prefix to model power loss."""


class MemFile(io.StringIO):
    """In-memory text file that participates in :class:`RecordingFs`'s fd table."""

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
    - A path already durable from a *prior* write is NOT auto-promoted on dir
      fsync: only paths in ``_file_content_synced`` (this generation's file
      fsync, or a hardlink that inherited it) may enter ``_durable``.
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

    def open(self, path: Path, mode: str = "w", *, encoding: str = "utf-8") -> MemFile:
        path = Path(path)
        self._record("open", path, mode)
        fh = MemFile(path, self, mode)
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
                elif p in self._file_content_synced:
                    # Promote only when this generation's bytes were file-fsynced
                    # (or inherited via hardlink from a content-synced source).
                    # Prior durability of the same path does NOT auto-promote a rewrite.
                    self._durable[p] = self.files.get(p, content)
                # else: refuse — crash recovery keeps prior durable content or absence.
                del self._pending_dir[p]
        elif fd in self._file_fds:
            path = self._file_fds[fd]
            self._record("fsync_file", path)
            obj = self._fds.get(fd)
            if isinstance(obj, MemFile) and not obj.closed:
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
