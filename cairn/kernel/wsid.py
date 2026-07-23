"""Workspace UUID — uncommitted local identity for multi-factory host units (D9 / W3).

One factory = one workspace. Host units (launchd labels, systemd unit stems) embed
the first 8 hex chars of a durable UUID so N factories on one machine never collide.
The id lives in ``<ws>/.cairn/workspace-id`` (gitignored, minted once).

Machine registry (FACTORY-PLAN §6 W3): ``~/.cairn/workspace-registry.json`` maps
uuid-hex → canonical resolved workspace path. A ``cp -r`` of a workspace that
carries the same ws-id file is detected on first ``workspace_id()`` call: the
registry maps that UUID to a *different* path, so the copy is re-minted and
registered under a fresh UUID.
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from pathlib import Path
from typing import Any

from cairn.kernel.durafs import atomic_write_text, exclusive_create

WORKSPACE_ID_REL = Path(".cairn") / "workspace-id"
REGISTRY_REL = Path(".cairn") / "workspace-registry.json"

# Process-local cache keyed by resolved workspace path string.
_CACHE: dict[str, str] = {}

# Test seam: when set, overrides the default machine registry path so unit tests
# never touch the real ``~/.cairn/workspace-registry.json``.
_REGISTRY_PATH_OVERRIDE: Path | None = None


def default_registry_path(*, home: Path | None = None) -> Path:
    """Machine-level registry path: ``~/.cairn/workspace-registry.json``."""
    if _REGISTRY_PATH_OVERRIDE is not None:
        return Path(_REGISTRY_PATH_OVERRIDE)
    base = Path(home) if home is not None else Path.home()
    return base / REGISTRY_REL


def workspace_id(
    ws: Path,
    *,
    _reset: bool = False,
    registry_path: Path | None = None,
    home: Path | None = None,
) -> str:
    """Read or mint the workspace UUID (uuid4 hex, 32 chars). Cached per process.

    - No ws-id file → mint via :func:`exclusive_create` (O_EXCL: concurrent first
      mints converge on one UUID) + register.
    - ws-id present and registry maps it to this path (or unregistered) → confirm
      registration, use it.
    - ws-id present and registry maps it to a *different* path → copy detected →
      re-mint, rewrite ws-id, register the new UUID for this path.

    ``registry_path`` / ``home`` are injectable for tests (never depend on the
    real ``~/.cairn``). Pass ``_reset=True`` only from tests to clear the process
    cache.
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

    reg_path = (
        Path(registry_path)
        if registry_path is not None
        else default_registry_path(home=home)
    )
    path = ws / WORKSPACE_ID_REL
    existing = _read_ws_id_file(path)

    if existing is not None:
        registered = _registry_lookup(reg_path, existing)
        if registered is not None and not _same_resolved_path(registered, key):
            # Copy: same UUID, different path → re-mint (overwrite the copied id).
            wid = uuid.uuid4().hex
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, wid + "\n")
            _registry_register(reg_path, wid, key)
            _CACHE[key] = wid
            return wid
        # Unregistered or already ours — confirm registration.
        _registry_register(reg_path, existing, key)
        _CACHE[key] = existing
        return existing

    # No file (or corrupt): exclusive mint so concurrent first-syncs converge.
    wid = _mint_exclusive(path)
    _registry_register(reg_path, wid, key)
    _CACHE[key] = wid
    return wid


def _read_ws_id_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip().replace("-", "").lower()
    except OSError:
        return None
    if len(text) >= 8 and all(c in "0123456789abcdef" for c in text):
        return text
    return None


def _mint_exclusive(path: Path) -> str:
    """Mint a UUID into ``path`` with O_EXCL; loser re-reads the winner's value.

    Concurrent first-syncs on the same workspace: exactly one writer creates the
    file; others observe FileExists and adopt the winner's UUID (I3).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Retry a few times if we lose the race then fail to re-read (rare).
    for _ in range(8):
        candidate = uuid.uuid4().hex
        if exclusive_create(path, candidate + "\n"):
            return candidate
        # Loser: adopt whatever is on disk now.
        existing = _read_ws_id_file(path)
        if existing is not None:
            return existing
        # Empty/corrupt mid-write window — try again.
    # Last resort: force a write (should be unreachable under normal FS).
    forced = uuid.uuid4().hex
    atomic_write_text(path, forced + "\n")
    return forced


def _same_resolved_path(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a == b


def _registry_lookup(reg_path: Path, wid: str) -> str | None:
    data = _read_registry(reg_path)
    val = data.get(wid)
    return str(val) if isinstance(val, str) and val else None


def _read_registry(reg_path: Path) -> dict[str, str]:
    reg_path = Path(reg_path)
    if not reg_path.is_file():
        return {}
    try:
        raw = json.loads(reg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str) and k and v:
            out[k.replace("-", "").lower()] = v
    return out


def _registry_register(
    reg_path: Path,
    wid: str,
    ws_path: str,
) -> None:
    """Upsert ``wid → ws_path`` under a flock.

    Concurrent registry writers serialize on ``workspace-registry.json.lock`` so
    two first-syncs never corrupt the JSON (C2). Never deletes another UUID's
    registration (the original of a copied workspace keeps its own entry).
    """
    reg_path = Path(reg_path)
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = reg_path.with_name(reg_path.name + ".lock")
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+", encoding="utf-8") as lock_fh:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        except OSError:
            # Best-effort: proceed without lock on platforms that refuse flock.
            pass
        try:
            data = _read_registry(reg_path)
            clean_wid = wid.replace("-", "").lower()
            # If another UUID already maps to this path (re-mint after copy), drop
            # only THAT stale mapping for this path — never another workspace's.
            stale = [
                k
                for k, v in data.items()
                if k != clean_wid and _same_resolved_path(v, ws_path)
            ]
            for k in stale:
                del data[k]
            data[clean_wid] = ws_path
            atomic_write_text(
                reg_path,
                json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            )
        finally:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


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
