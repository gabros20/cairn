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
  point owns dedupe: ``claim`` hard-links ``candidate`` into ``.claim/`` and unlinks the
  original — two concurrent claimers of one file can never both win, and losing the race
  (``FileNotFoundError``) means skip, never raise. A name already occupied in ``.claim/``
  (a stuck claim from an earlier crash) is never overwritten — a new candidate sharing
  that name gets a ``-v2`` suffix instead, same as ``consume``'s collision handling.
  Neither ``claim`` nor ``consume`` ever dereferences a symlinked event file: the
  symlink itself moves through the ledger, its target's bytes untouched. ``consume``
  retires a claim into ``.done/`` (or deletes it, per ``on_done``) on success, into
  ``.failed/`` on failure — a claim left behind in ``.claim/`` after a crash is surfaced
  by :func:`stuck_claims` for the operator to re-drop or discard, never auto-retried.

No CLI wiring, no renderers, no Runner effects here — later tasks build on this pure
core. stdlib + pyyaml only; no hidden clock, no network, no resident process.
"""

from __future__ import annotations

import errno
import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import yaml

from cairn.kernel.errors import CairnError, ConfigError
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
    """The resolved absolute watch directory for ``trigger``.

    ``_validate_watch`` at parse time only inspects the ``watch:`` STRING (rejects
    absolute paths and a literal ``..`` component) — it cannot see through a symlink. A
    ``watch:`` that is lexically clean but points, via a workspace-internal symlink,
    outside the workspace root would otherwise resolve cleanly here and hand back a
    directory nothing upstream ever validated. Resolving the workspace root ONCE and
    checking the resolved watch dir stays under it (mirrors ``artifacts.py``'s
    ``_assert_contained``) closes that gap: an escape via symlink is a ConfigError
    naming the trigger and the offending path, not a silent escape.
    """
    workspace_dir = Path(workspace_dir)
    real_workspace = workspace_dir.resolve()
    resolved = (workspace_dir / trigger.watch).resolve()
    if resolved != real_workspace and real_workspace not in resolved.parents:
        _fail(
            f"trigger {trigger.name!r}: 'watch' escapes the workspace via symlink: "
            f"{trigger.watch!r} resolves to {resolved}",
            workspace_dir / TRIGGERS_YAML,
        )
    return resolved


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


def _hardlink(src: Path, dest: Path) -> None:
    """Hard-link ``src`` into ``dest`` without ever dereferencing a symlinked ``src``.

    ``os.link``'s default ``follow_symlinks=True`` would resolve a symlinked event file
    and link its TARGET, not the symlink — a scan-accepted symlink (``Path.is_file()``
    follows symlinks, so ``scan_candidates`` happily accepts one) would then silently
    ledger the wrong bytes and orphan the real target outside the watch dir entirely.
    Passing ``follow_symlinks=False`` links the symlink itself, so it moves through the
    ledger intact and its target is never read here.

    ``FileNotFoundError`` (src vanished) and ``FileExistsError`` (dest already taken)
    pass through unchanged for the caller's own race/collision handling. Anything this
    layer can't handle safely becomes a :class:`CairnError` — a clear error beats a
    wrong or corrupted file:

    - a platform whose ``os.link`` can't target a symlink without dereferencing it
      (checked via ``os.supports_follow_symlinks``), when ``src`` actually is one;
    - ``src``/``dest`` on different filesystems (``EXDEV``) — a hard link can never
      cross a filesystem boundary, symlink or not.
    """
    if os.link in os.supports_follow_symlinks:
        link_kwargs: dict[str, bool] = {"follow_symlinks": False}
    elif src.is_symlink():
        raise CairnError(
            f"cannot link symlinked event {src} into {dest}: this platform's os.link "
            "cannot target a symlink without dereferencing it"
        )
    else:
        link_kwargs = {}
    try:
        os.link(src, dest, **link_kwargs)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise CairnError(
                f"cannot link {src} into {dest}: source and destination are on "
                "different filesystems (cross-device link)"
            ) from exc
        raise


def claim(watch_abs: Path, candidate: Path) -> Path | None:
    """Claim ``candidate`` by hard-linking it into ``<watch_abs>/.claim/`` and unlinking
    the original — never ``Path.rename``, because POSIX rename SILENTLY REPLACES an
    existing destination. A stuck claim left behind by an earlier crash is exactly the
    operator evidence :func:`stuck_claims` exists to surface; a rename-based claim would
    let a same-named new candidate destroy it with no error and no trace.

    ``os.link`` (via :func:`_hardlink`) fails atomically with ``FileExistsError`` when
    the destination name is taken, so a genuine name collision — a different event that
    happens to share a filename with something already sitting in ``.claim/`` — gets the
    same ``-v2`` collision-suffix treatment :func:`_place` uses for ``.done``/
    ``.failed``, rather than an overwrite: both the stuck claim and the new one survive
    under distinct names.

    Losing the race to claim ``candidate`` itself (a concurrent claimer already won and
    moved it) surfaces as ``FileNotFoundError`` — caught here and turned into ``None``,
    never raised, exactly as the ``Path.rename`` version did. A ``FileExistsError`` whose
    destination turns out to already be a hard link to ``candidate`` itself (a concurrent
    claimer's link narrowly beat ours to the very same source/name) is also a lost race,
    not a name collision — detected via ``_links_same_source`` before falling through to
    the ``-v2`` suffix path.
    """
    watch_abs = Path(watch_abs)
    candidate = Path(candidate)
    claim_dir = watch_abs / ".claim"
    claim_dir.mkdir(parents=True, exist_ok=True)
    name = candidate.name
    stem, ext = Path(name).stem, Path(name).suffix
    dest_name = name
    suffix = 1
    while True:
        dest = claim_dir / dest_name
        try:
            _hardlink(candidate, dest)
        except FileNotFoundError:
            return None
        except FileExistsError:
            if _links_same_source(candidate, dest):
                candidate.unlink(missing_ok=True)
                return None
            suffix += 1
            dest_name = f"{stem}-v{suffix}{ext}"
            continue
        break
    candidate.unlink()
    return dest


def _links_same_source(candidate: Path, dest: Path) -> bool:
    """Whether ``dest`` (which just refused a new hard link under this name) is already
    a hard link to ``candidate`` itself, rather than an unrelated file that merely shares
    a name. ``lstat`` (not ``stat``) compares by the symlink's own inode when
    ``candidate`` is one, matching the ``follow_symlinks=False`` link :func:`_hardlink`
    makes."""
    try:
        c = candidate.lstat()
    except FileNotFoundError:
        return True  # candidate already gone: someone else clearly won it
    try:
        d = dest.lstat()
    except FileNotFoundError:
        return False  # dest vanished between the FileExistsError and here: retry
    return (c.st_dev, c.st_ino) == (d.st_dev, d.st_ino)


def _place(claim_path: Path, dest_dir: Path, name: str) -> Path:
    """Hard-link ``claim_path`` into ``dest_dir`` under ``name`` (never dereferencing a
    symlinked claim — see :func:`_hardlink`), never overwriting an existing file: a name
    collision gets a ``-v2``, ``-v3``, ... suffix before the extension (the
    ``bootstrap_run`` convention — see ``walk.py``'s ``-v2`` collision handling).
    ``os.link`` fails atomically with ``FileExistsError`` when the target name is taken,
    so each attempt is race-safe even though the retry loop around it is not.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem, ext = Path(name).stem, Path(name).suffix
    dest_name = name
    suffix = 1
    while True:
        dest = dest_dir / dest_name
        try:
            _hardlink(claim_path, dest)
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
