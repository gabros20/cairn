"""Per-run secrets that authenticate gate decisions (SECURITY.md §6).

The run dir is the agent's write scope (codex gets ``--sandbox workspace-write`` over it),
so a decision file sitting inside it proves nothing about *who* wrote it. This module holds
the per-run secret that does: a 32-byte key minted once when the run is created and stored
in a user-level state dir **outside** any run's cwd, so an injected/compromised step cannot
read it (and therefore cannot forge a decision) unless the executor's sandbox is bypassed.

The key path is derived from the run id alone (``run_dir.name``), so the writer (gatekit's
``_commit`` / ``answer_gate``), the verifier (``resolve_gate``), and the mint site
(``bootstrap_run``) all agree on it without plumbing the secret through call signatures.

The key file name is ``<run_id>-<disc>.key`` where ``disc`` is a hash of the run dir's
absolute path — so two runs that share a run_id in different workspaces get distinct keys.

Location ladder (first that applies):
    ``$XDG_STATE_HOME/cairn/gate-keys/<run_id>-<disc>.key``
    ``~/.local/state/cairn/gate-keys/<run_id>-<disc>.key``   (HOME set, XDG_STATE_HOME not)
    ``~/.cairn/gate-keys/<run_id>-<disc>.key``               (neither set)

The dir is created 0700 and each key file 0600. Stdlib only (secrets/hmac/hashlib/json).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path

_KEY_BYTES = 32

# A per-invocation manifest key (C9: step.id + cycle) can contain characters a filename
# can't (step ids may hold '/' or '.'; cycle renders as a bare int or "None") — collapse
# anything outside this safe set to '-'. The MAC binds run-disc + content, not the filename,
# so a sanitized-but-collided key is not a security issue, only a (harmless) path reuse.
_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9._-]")


def gate_keys_dir() -> Path:
    """The directory that holds per-run gate keys (see the location ladder above)."""
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg:
        return Path(xdg) / "cairn" / "gate-keys"
    home = os.environ.get("HOME", "").strip()
    if home:
        return Path(home) / ".local" / "state" / "cairn" / "gate-keys"
    return Path.home() / ".cairn" / "gate-keys"


def _run_disc(run_dir: Path) -> str:
    """A stable per-run discriminator: 12 hex of sha256 over the run dir's ABSOLUTE path.

    ``run_id`` (``run_dir.name``) is timestamp-derived and collision-suffixed only WITHIN one
    ``runs_root`` — two runs in different workspaces can share a run_id. Binding the absolute
    dir path distinguishes them, so their keys and MACs never collide. ``Path.resolve()`` is
    CWD-independent for an existing dir, so it is stable across ``run`` and ``resume``.
    """
    abspath = os.fspath(Path(run_dir).resolve())
    return hashlib.sha256(abspath.encode("utf-8")).hexdigest()[:12]


def _key_path(run_dir: Path) -> Path:
    # The discriminator is in the filename too, so a shared run_id across workspaces mints two
    # distinct key files rather than the second run silently reusing the first's secret.
    return gate_keys_dir() / f"{Path(run_dir).name}-{_run_disc(run_dir)}.key"


def ensure_run_key(run_dir: Path) -> bytes:
    """Return the run's secret, minting + persisting a fresh 0600 key file if absent.

    Idempotent: an existing key file is REUSED (regenerating would invalidate every gate MAC
    the run has already committed). Called at run mint (``bootstrap_run``) and, as a safety
    net, by the writers — never by the verifier, which must fail safe on a missing key.
    """
    path = _key_path(run_dir)
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
    # O_EXCL (not O_TRUNC) so the mint won't follow/truncate a pre-planted symlink in the key
    # dir — defense in depth even though that dir lives outside the agent's write scope. A stale
    # tmp from a crashed mint is unlinked and retried once.
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        os.unlink(tmp)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, secret)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return secret


def load_run_key(run_dir: Path) -> bytes | None:
    """Return the run's secret, or None if the key file is missing/unreadable/empty.

    Read-only: it NEVER mints. A None here means "cannot authenticate" — the verifier treats
    that as tamper (unresolved), never as "trust the file".
    """
    return _read_key(_key_path(run_dir))


def _read_key(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data or None


def _canonical_payload(run_disc: str, gate: str, choice, by, at) -> bytes:
    """The exact byte serialization both writer and verifier sign — they must never drift.

    ``gate`` (the name) is IN the payload so a valid decision for gate A cannot be replayed at
    gate B's path; ``run`` (the per-run discriminator) is in it so a decision cannot be replayed
    into a different run that happens to share a run_id. Sorted keys + tight separators pin one
    canonical form across Python builds.
    """
    return json.dumps(
        {"run": run_disc, "gate": gate, "choice": choice, "by": by, "at": at},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_mac(secret: bytes, run_dir: Path, gate: str, choice, by, at) -> str:
    """HMAC-SHA256 hex digest over the canonical ``{run,gate,choice,by,at}`` payload."""
    return hmac.new(
        secret, _canonical_payload(_run_disc(run_dir), gate, choice, by, at), hashlib.sha256
    ).hexdigest()


# --------------------------------------------------------------------------- #
# Guard manifests — the SAME threat model as gate decisions. A guard manifest
# (the decls a command is checked against) sitting inside the run dir proves
# nothing about who wrote it, so it lives here (outside the agent's cwd) and is
# MAC-authenticated with the per-run secret. See guards.py.
# --------------------------------------------------------------------------- #


def guard_manifests_dir() -> Path:
    """The directory holding per-run guard manifests — a sibling of :func:`gate_keys_dir`, and
    like it OUTSIDE any run's cwd, so a sandboxed executor (codex ``workspace-write`` over the
    run dir) cannot rewrite the guard decls its own commands are checked against."""
    return gate_keys_dir().parent / "guard-manifests"


def guard_manifest_path(run_dir: Path, layer: str, *, key: str | None = None) -> Path:
    """Path to a run's ``layer`` (``"hook"`` / ``"shim"``) guard manifest in the protected dir,
    keyed by run_id + the absolute-path discriminator exactly like the gate key file.

    ``key``, when given, is an OPTIONAL per-invocation token (C9: ``walk.py``'s
    ``_active_guard_manifest`` passes ``f"{step.id}-c{cycle}"``) appended to the filename, so a
    runtime-``when`` guard's active set — which can differ per step/cycle — gets its own manifest
    rather than racing a shared one under ``parallel:`` (GUARD-WHEN-PLAN.md §6). Sanitized to a
    filesystem-safe token; the MAC binds run-disc + content, not the filename, so a per-invocation
    filename is safe. No ``key`` → the original static/once-per-run path, unchanged."""
    base = f"{Path(run_dir).name}-{_run_disc(run_dir)}-{layer}"
    if key is not None:
        base = f"{base}-{_SAFE_KEY_RE.sub('-', key)}"
    return guard_manifests_dir() / f"{base}.json"


def compute_content_mac(secret: bytes, run_dir: Path, content: object) -> str:
    """HMAC-SHA256 hex over a JSON-canonical ``content`` bound to this run — the run
    discriminator is IN the signed payload, so a manifest cannot be replayed into a different run
    that shares a run_id. Same key and primitive as the gate MACs, one canonical form pinned by
    ``sort_keys`` + tight separators."""
    payload = json.dumps(
        {"run": _run_disc(run_dir), "content": content},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()
