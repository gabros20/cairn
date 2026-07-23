"""Workspace UUID — uncommitted local identity for multi-factory host units (D9 / W3).

One factory = one workspace. Host units (launchd labels, systemd unit stems) embed
the first 8 hex chars of a durable UUID so N factories on one machine never collide.
The id lives in ``<ws>/.cairn/workspace-id`` (gitignored, minted once).
"""

from __future__ import annotations

import uuid
from pathlib import Path

from cairn.kernel.durafs import atomic_write_text

WORKSPACE_ID_REL = Path(".cairn") / "workspace-id"

# Process-local cache keyed by resolved workspace path string.
_CACHE: dict[str, str] = {}


def workspace_id(ws: Path, *, _reset: bool = False) -> str:
    """Read or mint the workspace UUID (uuid4 hex, 32 chars). Cached per process.

    Minted once via :func:`atomic_write_text` into ``<ws>/.cairn/workspace-id``.
    Pass ``_reset=True`` only from tests to clear the process cache.
    """
    if _reset:
        _CACHE.clear()
    ws = Path(ws)
    try:
        key = str(ws.resolve())
    except OSError:
        key = str(ws)
    if key in _CACHE:
        return _CACHE[key]

    path = ws / WORKSPACE_ID_REL
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip().replace("-", "").lower()
        if len(text) >= 8 and all(c in "0123456789abcdef" for c in text):
            _CACHE[key] = text
            return text

    minted = uuid.uuid4().hex
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, minted + "\n")
    _CACHE[key] = minted
    return minted


def ws8(ws: Path | str) -> str:
    """First 8 hex chars of the workspace UUID (label segment).

    Accepts a workspace path, or a bare hex id string (already-known UUID).
    """
    if isinstance(ws, str) and not (Path(ws).exists() or "/" in ws or ws.startswith(".")):
        cleaned = ws.replace("-", "").lower()
        if len(cleaned) >= 8 and all(c in "0123456789abcdef" for c in cleaned[:8]):
            return cleaned[:8]
    return workspace_id(Path(ws))[:8]


def label_prefix_for(ws: Path | str) -> str:
    """launchd label prefix: ``io.cairn.<ws8>.`` (schedules + reconcile beat)."""
    return f"io.cairn.{ws8(ws)}."
