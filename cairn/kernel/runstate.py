"""run.json — the per-run manifest — plus the run's advisory lock.

`run.json` is one of cairn's two state authorities on disk (the other is the trail). Every
read validates it against the pinned `cairn:run` schema and every write is atomic (tmp +
`os.replace`), so a reader never sees a half-written manifest and an invalid mutation never
lands. `run_lock` is the flock that stops two `cairn resume`s from interleaving on one run
(SECURITY.md §5); the loser is told which PID holds it.

stdlib + jsonschema only. No threads, no daemons.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from jsonschema import Draft202012Validator

from cairn.kernel import durafs
from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.schemas import get_schema

RUN_JSON = "run.json"
LOCK_NAME = ".cairn.lock"


class RunExistsError(CairnError):
    """`create_run` refused because the run dir already exists (variant policy is the caller's)."""


class LockHeldError(CairnError):
    """The run's advisory lock is already held by another process (SECURITY.md §5)."""

    def __init__(self, message: str, *, pid: int | None = None) -> None:
        super().__init__(message)
        self.pid = pid


def _validator() -> Draft202012Validator:
    return Draft202012Validator(get_schema("run"))


def _validate(doc: dict) -> None:
    """Validate against cairn:run, raising ConfigError with the first violation's message."""
    errors = sorted(_validator().iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        raise ConfigError(f"run.json is invalid: {errors[0].message}")


def _atomic_write(path: Path, doc: dict) -> None:
    """Durably replace `path` with `doc` via :func:`cairn.kernel.durafs.atomic_write_json`.

    run.json is a state authority (like the trail). Atomic alone isn't enough — without
    fsyncing the tmp file before the rename, a power loss can land an empty or truncated
    manifest. The directory fsync makes the rename itself durable. The single fsync
    discipline lives in ``durafs`` (T0, D2); this wrapper keeps the local name so call
    sites stay stable.
    """
    durafs.atomic_write_json(path, doc)


def create_run(runs_root: Path, run_id: str, payload: dict) -> Path:
    """Create `runs_root/run_id/` and write a validated run.json into it.

    A pre-existing run dir raises RunExistsError (the -v2 variant decision belongs to the
    caller). An invalid payload raises ConfigError and leaves nothing behind.
    """
    runs_root = Path(runs_root)
    run_dir = runs_root / run_id
    try:
        run_dir.mkdir(parents=True)
    except FileExistsError as exc:
        raise RunExistsError(f"run dir already exists: {run_dir}") from exc

    try:
        _validate(payload)
    except ConfigError:
        run_dir.rmdir()  # nothing was written yet; don't leave a stub dir behind
        raise

    _atomic_write(run_dir / RUN_JSON, payload)
    return run_dir


def load_run(run_dir: Path) -> dict:
    """Read and validate `run_dir/run.json`."""
    doc = json.loads((Path(run_dir) / RUN_JSON).read_text(encoding="utf-8"))
    _validate(doc)
    return doc


def update_run(run_dir: Path, mutate: Callable[[dict], None]) -> dict:
    """read → mutate(doc) in place → validate → atomic replace. Returns the new doc.

    An invalid mutation raises ConfigError and leaves the on-disk file untouched.
    """
    path = Path(run_dir) / RUN_JSON
    doc = json.loads(path.read_text(encoding="utf-8"))
    mutate(doc)
    _validate(doc)
    _atomic_write(path, doc)
    return doc


def node_status(run_dict: dict, node_id: str) -> str | None:
    """The recorded status of one node, or None if the node isn't in the map yet."""
    return run_dict.get("nodes", {}).get(node_id, {}).get("status")


def set_node_status(
    run_dict: dict,
    node_id: str,
    status: str,
    at: str,
    cycles: int | None = None,
) -> None:
    """Set a node's status/at (and optional cycle count) in the run dict, in place."""
    entry: dict = {"status": status, "at": at}
    if cycles is not None:
        entry["cycles"] = cycles
    run_dict.setdefault("nodes", {})[node_id] = entry


@contextmanager
def run_lock(run_dir: Path) -> Iterator[None]:
    """Exclusive advisory lock on `run_dir/.cairn.lock` (flock, non-blocking).

    Already held → LockHeldError carrying the holder's PID. While held, this process's PID
    is written into the lockfile so a contender can name the holder.
    """
    lock_path = Path(run_dir) / LOCK_NAME
    lock_path.touch(exist_ok=True)
    fh = lock_path.open("r+", encoding="utf-8")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.seek(0)
            holder = fh.read().strip()
            fh.close()
            pid = int(holder) if holder.isdigit() else None
            raise LockHeldError(f"run is held by PID {holder or '?'}", pid=pid) from exc

        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        if not fh.closed:
            fh.close()
