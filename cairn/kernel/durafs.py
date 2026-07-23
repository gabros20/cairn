"""Durable filesystem primitives — one fsync discipline for the kernel (T0, D2).

Every state-authority write and every QTP move goes through this module so crash
recovery has a single ordering rule: file contents are fsynced before the
directory entry that publishes them, and a move's destination parent is fsynced
before the source is unlinked (the new location is durable before the old
disappears).

Concurrency: ``atomic_write_json`` / ``atomic_write_text`` use a fixed tmp name
(``<path>.tmp``) with no PID/random suffix. Callers must serialize writers to
the same destination path (e.g. ``runstate.run_lock`` before writing
``run.json``). This module does not provide locking.

POLICY (EXDEV, symlink follow, collision suffixes) stays with callers. This
module only does link/unlink/write + fsync; it never rewrites OSError semantics.

Tests inject a small ``_FsOps`` seam (keyword-only ``fs=``) rather than
monkeypatching ``os.*`` (D10). Production call sites leave ``fs`` unset.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol, TextIO


class _FsOps(Protocol):
    """Internal test seam: real OS ops by default; fakes record/crash/lose suffixes.

    Not a public API — sufficient for ordering and replay-loss tests only.
    """

    def open(self, path: Path, mode: str = "w", *, encoding: str = "utf-8") -> TextIO: ...

    def replace(self, src: Path, dst: Path) -> None: ...

    def link(self, src: Path, dst: Path) -> None: ...

    def unlink(self, path: Path) -> None: ...

    def fsync(self, fd: int) -> None: ...

    def open_dir(self, path: Path) -> int: ...

    def close(self, fd: int) -> None: ...

    def exclusive_create(self, path: Path, text: str = "") -> bool: ...


class _OsFs:
    """Default backend: delegates to ``os`` / ``Path`` at call time (monkeypatch-visible)."""

    def open(self, path: Path, mode: str = "w", *, encoding: str = "utf-8") -> TextIO:
        return Path(path).open(mode, encoding=encoding)

    def replace(self, src: Path, dst: Path) -> None:
        os.replace(src, dst)

    def link(self, src: Path, dst: Path) -> None:
        # Prefer not following symlinks so QTP ledger moves transport a symlink
        # inode intact (callers that need the target resolve first — policy stays
        # with callers; this is only the mechanical default for platforms that
        # expose follow_symlinks on link).
        if os.link in os.supports_follow_symlinks:
            os.link(src, dst, follow_symlinks=False)
        else:
            os.link(src, dst)

    def unlink(self, path: Path) -> None:
        os.unlink(path)

    def fsync(self, fd: int) -> None:
        os.fsync(fd)

    def open_dir(self, path: Path) -> int:
        return os.open(path, os.O_RDONLY)

    def close(self, fd: int) -> None:
        os.close(fd)

    def exclusive_create(self, path: Path, text: str = "") -> bool:
        """O_CREAT|O_EXCL create; True if created, False if the name is taken."""
        path = Path(path)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            return False
        try:
            if text:
                os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return True


_OS_FS = _OsFs()


def _resolve(fs: _FsOps | None) -> _FsOps:
    return fs if fs is not None else _OS_FS


def fsync_dir(path: Path, *, fs: _FsOps | None = None) -> None:
    """Fsync the directory at ``path`` so recent create/rename/unlink entries stick."""
    ops = _resolve(fs)
    dir_fd = ops.open_dir(Path(path))
    try:
        ops.fsync(dir_fd)
    finally:
        ops.close(dir_fd)


def atomic_write_text(path: Path, text: str, *, fs: _FsOps | None = None) -> None:
    """Durably replace ``path`` with ``text``: tmp write → file fsync → replace → dir fsync.

    The tmp name is fixed (``path`` + ``.tmp``); callers must serialize concurrent
    writers to the same destination (e.g. ``run_lock`` for ``run.json``).
    """
    ops = _resolve(fs)
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with ops.open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        ops.fsync(fh.fileno())
    ops.replace(tmp, path)
    fsync_dir(path.parent, fs=ops)


def atomic_write_json(path: Path, doc: dict, *, fs: _FsOps | None = None) -> None:
    """Durably replace ``path`` with JSON ``doc`` (indent=2, ensure_ascii=False).

    Same discipline as the former ``runstate._atomic_write``: tmp + file fsync +
    ``os.replace`` + parent-dir fsync. Fixed tmp name — callers serialize per-path
    (e.g. ``run_lock`` before writing ``run.json``).
    """
    atomic_write_text(
        path,
        json.dumps(doc, indent=2, ensure_ascii=False),
        fs=fs,
    )


def durable_link(src: Path, dest: Path, *, fs: _FsOps | None = None) -> None:
    """Hard-link ``src`` to ``dest``, then fsync ``dest``'s parent directory.

    ``OSError`` (including ``EXDEV`` and ``FileExistsError``) propagates untouched —
    callers own policy.
    """
    ops = _resolve(fs)
    src, dest = Path(src), Path(dest)
    ops.link(src, dest)
    fsync_dir(dest.parent, fs=ops)


def durable_unlink(path: Path, *, fs: _FsOps | None = None) -> None:
    """Unlink ``path``, then fsync its parent directory."""
    ops = _resolve(fs)
    path = Path(path)
    ops.unlink(path)
    fsync_dir(path.parent, fs=ops)


def durable_move(src: Path, dest: Path, *, fs: _FsOps | None = None) -> None:
    """Move via link-then-unlink: dest parent fsynced before src disappears (QTP).

    Order: link → fsync(dest parent) → unlink(src) → fsync(src parent).
    Cross-device (``EXDEV``) and platform errors propagate untouched.
    """
    ops = _resolve(fs)
    src, dest = Path(src), Path(dest)
    durable_link(src, dest, fs=ops)
    durable_unlink(src, fs=ops)


def exclusive_create(path: Path, text: str = "", *, fs: _FsOps | None = None) -> bool:
    """Create ``path`` with O_CREAT|O_EXCL (or the fs seam's equivalent).

    Returns True when this caller created the file, False when the name was already
    taken — the atomic mutual-exclusion primitive for identity reservations
    (``.claim/.ids/<identity>``, FACTORY-PLAN T1). Content is file-fsynced then the
    parent directory is fsynced so the reservation survives a crash that follows
    a successful return. Caller ensures the parent directory exists.
    """
    ops = _resolve(fs)
    path = Path(path)
    if not ops.exclusive_create(path, text):
        return False
    fsync_dir(path.parent, fs=ops)
    return True
