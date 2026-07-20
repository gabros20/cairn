"""triggerkit — event triggers without a listener (docs/TRIGGERS-PLAN.md).

cairn never owns a resident process; the host's own file-watch facility (launchd
``WatchPaths``, systemd ``.path`` units) fires ``cairn trigger run <name>``, exactly as
the host clock fires ``cairn schedule run <name>`` (schedkit). An event is a file — a
payload dropped into a watched inbox directory, the filesystem staying the single
authority — and this module is ONE synchronous pass over that inbox, never a watch/poll
loop or daemon of its own (TRIGGERS-PLAN.md §0 doctrine: no daemon, no listener).

- :func:`load_triggers` — parse + validate ``triggers.yaml`` into typed :class:`Trigger`
  objects, with precise :class:`ConfigError`\\s (unknown keys, unknown pipeline, an
  absolute/escaping ``watch:``) — mirrors :func:`schedkit.load_schedules`. Unlike
  ``schedules.yaml``, a missing ``triggers.yaml`` is not an error: no file means no
  triggers declared.
- :func:`watch_dir` — resolve a trigger's workspace-relative ``watch:`` to an absolute
  directory.
- :func:`scan_candidates` — top-level files in the watch dir matching ``glob``; dotfiles
  and subdirectories (including the ``.claim``/``.done``/``.failed`` ledger dirs below)
  are always excluded.
- :func:`claim` / :func:`consume` / :func:`stuck_claims` — the at-most-once ledger
  (TRIGGERS-PLAN.md §2). The host watcher may coalesce or duplicate firings, so the entry
  point owns dedupe: ``claim`` is an atomic same-filesystem rename into ``.claim/`` —
  two concurrent claimers of one file can never both win, and losing the race
  (``FileNotFoundError``) means skip, never raise. ``consume`` retires a claim into
  ``.done/`` (or deletes it, per ``on_done``) on success, into ``.failed/`` on failure —
  a claim left behind in ``.claim/`` after a crash is surfaced by :func:`stuck_claims`
  for the operator to re-drop or discard, never auto-retried.

No CLI wiring, no renderers, no Runner effects here — later tasks build on this pure
core. stdlib + pyyaml only; no hidden clock, no network, no resident process.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import yaml

from cairn.kernel.errors import ConfigError
from cairn.kernel.types import Finding

TRIGGERS_YAML = "triggers.yaml"

_TRIGGER_KEYS = frozenset({"pipeline", "watch", "param", "glob", "on_done"})
_ON_DONE_VALUES = frozenset({"done", "delete"})


# --------------------------------------------------------------------------- #
# Typed model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Trigger:
    """One declared trigger: a pipeline to fire, a directory to watch, and how a claimed
    event's outcome is retired. ``watch`` is stored verbatim as authored (workspace-
    relative); resolve it with :func:`watch_dir`."""

    name: str
    pipeline: str
    watch: str
    param: str = "event"
    glob: str = "*"
    on_done: str = "done"  # "done" | "delete"


def _fail(message: str, file: Path) -> NoReturn:
    raise ConfigError(message, findings=[Finding("error", message)], file=str(file))


# --------------------------------------------------------------------------- #
# load_triggers
# --------------------------------------------------------------------------- #


def load_triggers(workspace_dir: Path) -> dict[str, Trigger]:
    """Load ``<workspace_dir>/triggers.yaml`` into name → :class:`Trigger`.

    A missing file means no triggers declared (returns ``{}``) — unlike
    ``schedules.yaml``, triggers are optional infrastructure. Raises :class:`ConfigError`
    (naming the offending trigger/field) on malformed YAML, a non-mapping top level,
    unknown keys, an unknown pipeline, or a ``watch:`` that is absolute or escapes the
    workspace.
    """
    workspace_dir = Path(workspace_dir)
    file = workspace_dir / TRIGGERS_YAML
    if not file.is_file():
        return {}

    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        _fail(f"triggers.yaml is not valid YAML: {exc}", file)

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _fail("triggers.yaml must be a mapping of name → trigger", file)

    triggers: dict[str, Trigger] = {}
    for name, entry in raw.items():
        triggers[str(name)] = _parse_trigger(str(name), entry, workspace_dir, file)
    return triggers


def _parse_trigger(name: str, entry: Any, workspace_dir: Path, file: Path) -> Trigger:
    if not isinstance(entry, dict):
        _fail(f"trigger {name!r} must be a mapping with 'pipeline' and 'watch'", file)
    for key in entry:
        if key not in _TRIGGER_KEYS:
            _fail(
                f"trigger {name!r}: unknown key {key!r} "
                f"(allowed: {', '.join(sorted(_TRIGGER_KEYS))})",
                file,
            )

    pipeline = entry.get("pipeline")
    if not isinstance(pipeline, str) or not pipeline.strip():
        _fail(f"trigger {name!r} requires a non-empty string 'pipeline'", file)
    pfile = workspace_dir / "pipelines" / f"{pipeline}.yaml"
    if not pfile.is_file():
        _fail(f"trigger {name!r}: unknown pipeline {pipeline!r} (no {pfile})", file)

    watch = entry.get("watch")
    if not isinstance(watch, str) or not watch.strip():
        _fail(f"trigger {name!r} requires a non-empty string 'watch'", file)
    _validate_watch(name, watch, file)

    param = entry.get("param", "event")
    if not isinstance(param, str) or not param.strip():
        _fail(f"trigger {name!r}: 'param' must be a non-empty string, got {param!r}", file)

    glob = entry.get("glob", "*")
    if not isinstance(glob, str) or not glob:
        _fail(f"trigger {name!r}: 'glob' must be a non-empty string, got {glob!r}", file)

    on_done = entry.get("on_done", "done")
    if on_done not in _ON_DONE_VALUES:
        _fail(
            f"trigger {name!r}: 'on_done' must be one of "
            f"{sorted(_ON_DONE_VALUES)}, got {on_done!r}",
            file,
        )

    return Trigger(name=name, pipeline=pipeline, watch=watch, param=param, glob=glob, on_done=on_done)


def _validate_watch(name: str, watch: str, file: Path) -> None:
    """Reject a ``watch:`` that is absolute or that escapes the workspace via ``..``."""
    p = Path(watch)
    if p.is_absolute():
        _fail(f"trigger {name!r}: 'watch' must be workspace-relative, not absolute: {watch!r}", file)
    if ".." in p.parts:
        _fail(f"trigger {name!r}: 'watch' must not escape the workspace: {watch!r}", file)


# --------------------------------------------------------------------------- #
# Inbox scan
# --------------------------------------------------------------------------- #


def watch_dir(trigger: Trigger, workspace_dir: Path) -> Path:
    """The resolved absolute watch directory for ``trigger``."""
    return (Path(workspace_dir) / trigger.watch).resolve()


def scan_candidates(watch_abs: Path, glob: str) -> list[Path]:
    """Top-level files in ``watch_abs`` matching ``glob``, sorted.

    Never recurses; excludes directories and any name starting with ``.`` (which keeps
    the ``.claim``/``.done``/``.failed`` ledger dirs below out of the scan by
    construction). A watch dir that doesn't exist yet (no event has landed) scans empty.
    """
    watch_abs = Path(watch_abs)
    if not watch_abs.is_dir():
        return []
    return sorted(
        p
        for p in watch_abs.iterdir()
        if p.is_file() and not p.name.startswith(".") and fnmatch.fnmatch(p.name, glob)
    )


# --------------------------------------------------------------------------- #
# Claim / consume — the at-most-once ledger (TRIGGERS-PLAN.md §2)
# --------------------------------------------------------------------------- #


def claim(watch_abs: Path, candidate: Path) -> Path | None:
    """Atomically claim ``candidate`` by renaming it into ``<watch_abs>/.claim/``.

    ``Path.rename`` is atomic on the same filesystem, so two concurrent claimers of one
    file can never both succeed: the loser's source has already vanished underneath it,
    which surfaces as ``FileNotFoundError`` — caught here and turned into ``None``
    (lost the race), never raised.
    """
    watch_abs = Path(watch_abs)
    candidate = Path(candidate)
    claim_dir = watch_abs / ".claim"
    claim_dir.mkdir(parents=True, exist_ok=True)
    dest = claim_dir / candidate.name
    try:
        candidate.rename(dest)
    except FileNotFoundError:
        return None
    return dest


def _place(claim_path: Path, dest_dir: Path, name: str) -> Path:
    """Hard-link ``claim_path`` into ``dest_dir`` under ``name``, never overwriting an
    existing file: a name collision gets a ``-v2``, ``-v3``, ... suffix before the
    extension (the ``bootstrap_run`` convention — see ``walk.py``'s ``-v2`` collision
    handling). ``os.link`` fails atomically with ``FileExistsError`` when the target name
    is taken, so each attempt is race-safe even though the retry loop around it is not.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem, ext = Path(name).stem, Path(name).suffix
    dest_name = name
    suffix = 1
    while True:
        dest = dest_dir / dest_name
        try:
            os.link(claim_path, dest)
        except FileExistsError:
            suffix += 1
            dest_name = f"{stem}-v{suffix}{ext}"
            continue
        break
    claim_path.unlink()
    return dest


def consume(watch_abs: Path, claim_path: Path, *, ok: bool, on_done: str) -> Path | None:
    """Retire a claimed file: ``.done/`` (or delete, per ``on_done``) on success,
    ``.failed/`` on failure — a failed event is always moved aside, never left to
    retry-loop as a poison file."""
    watch_abs = Path(watch_abs)
    claim_path = Path(claim_path)
    if not ok:
        return _place(claim_path, watch_abs / ".failed", claim_path.name)
    if on_done == "delete":
        claim_path.unlink()
        return None
    return _place(claim_path, watch_abs / ".done", claim_path.name)


def stuck_claims(watch_abs: Path) -> list[Path]:
    """Files sitting in ``.claim/`` — a crash mid-run leaves its claim here, never
    auto-retried; the operator re-drops or discards it (surfaced by ``trigger list``)."""
    claim_dir = Path(watch_abs) / ".claim"
    if not claim_dir.is_dir():
        return []
    return sorted(p for p in claim_dir.iterdir() if p.is_file())
