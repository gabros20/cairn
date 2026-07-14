"""Per-run secrets that authenticate gate decisions (SECURITY.md §6).

The run dir is the agent's write scope (codex gets ``--sandbox workspace-write`` over it),
so a decision file sitting inside it proves nothing about *who* wrote it. This module holds
the per-run secret that does: a 32-byte key minted once when the run is created and stored
in a user-level state dir **outside** any run's cwd, so an injected/compromised step cannot
read it (and therefore cannot forge a decision) unless the executor's sandbox is bypassed.

The key path is derived from the run id alone (``run_dir.name``), so the writer (gatekit's
``_commit`` / ``answer_gate``), the verifier (``resolve_gate``), and the mint site
(``bootstrap_run``) all agree on it without plumbing the secret through call signatures.

Location ladder (first that applies):
    ``$XDG_STATE_HOME/cairn/gate-keys/<run_id>.key``
    ``~/.local/state/cairn/gate-keys/<run_id>.key``   (HOME set, XDG_STATE_HOME not)
    ``~/.cairn/gate-keys/<run_id>.key``               (neither set)

The dir is created 0700 and each key file 0600. Stdlib only (secrets/hmac/hashlib/json).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

_KEY_BYTES = 32


def gate_keys_dir() -> Path:
    """The directory that holds per-run gate keys (see the location ladder above)."""
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg:
        return Path(xdg) / "cairn" / "gate-keys"
    home = os.environ.get("HOME", "").strip()
    if home:
        return Path(home) / ".local" / "state" / "cairn" / "gate-keys"
    return Path.home() / ".cairn" / "gate-keys"


def _key_path(run_id: str) -> Path:
    return gate_keys_dir() / f"{run_id}.key"


def ensure_run_key(run_id: str) -> bytes:
    """Return the run's secret, minting + persisting a fresh 0600 key file if absent.

    Idempotent: an existing key file is REUSED (regenerating would invalidate every gate MAC
    the run has already committed). Called at run mint (``bootstrap_run``) and, as a safety
    net, by the writers — never by the verifier, which must fail safe on a missing key.
    """
    path = _key_path(run_id)
    existing = _read_key(path)
    if existing is not None:
        return existing

    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass  # best-effort tightening; a pre-existing looser dir is the operator's call

    secret = secrets.token_bytes(_KEY_BYTES)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, secret)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return secret


def load_run_key(run_id: str) -> bytes | None:
    """Return the run's secret, or None if the key file is missing/unreadable/empty.

    Read-only: it NEVER mints. A None here means "cannot authenticate" — the verifier treats
    that as tamper (unresolved), never as "trust the file".
    """
    return _read_key(_key_path(run_id))


def _read_key(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data or None


def _canonical_payload(gate: str, choice, by, at) -> bytes:
    """The exact byte serialization both writer and verifier sign — they must never drift.

    ``gate`` (the name) is IN the payload so a valid decision for gate A cannot be replayed at
    gate B's path. Sorted keys + tight separators pin one canonical form across Python builds.
    """
    return json.dumps(
        {"gate": gate, "choice": choice, "by": by, "at": at},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_mac(secret: bytes, gate: str, choice, by, at) -> str:
    """HMAC-SHA256 hex digest over the canonical ``{gate,choice,by,at}`` payload."""
    return hmac.new(secret, _canonical_payload(gate, choice, by, at), hashlib.sha256).hexdigest()
