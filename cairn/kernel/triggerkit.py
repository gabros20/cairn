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

- :func:`render_trigger_launchd` / :func:`render_trigger_systemd` — RENDER-ONLY functions
  returning the exact host-watcher text (a launchd ``WatchPaths`` plist; a systemd
  ``.path`` + ``.service`` pair), fully unit-testable offline — mirror schedkit's
  :func:`render_launchd` / :func:`render_systemd` conventions exactly. Argv is always the
  stable entry ``cairn trigger run <name> --workspace <ws>``, never the expanded per-file
  work, so editing ``triggers.yaml`` changes behavior without re-sync (the same property
  SCHEDULING.md documents for schedules). cron has no file-watch facility — the sync verb
  (a later task) refuses that backend rather than fake one here.

- :func:`sync_triggers` / :func:`remove_trigger` / :func:`list_installed_triggers` /
  :func:`run_trigger` — the effectful verbs, every side effect dependency-injected via a
  :class:`~cairn.kernel.proc.Runner` and explicit target dirs, mirroring schedkit's
  ``install``/``uninstall``/``list_installed``/``run_schedule`` exactly (same
  ``_require_dir``, ``_bad_backend``, managed-glob pruning). Two triggers-specific
  hardenings beyond that mirror: ``sync_triggers`` never writes or prunes a file it
  can't recognizably attribute to itself (a schedule named ``trigger-X`` and a trigger
  named ``X`` both render ``cairn-trigger-X.service`` — the T2 namespace alone cannot
  rule this out, so ownership is verified from the existing file's own rendered content
  before it is ever touched); and a repeated sync of an unchanged ``triggers.yaml`` is a
  true no-op — zero host-watcher calls, not just unwritten bytes. ``run_trigger`` is the
  function the host watcher's ``cairn trigger run <name>`` argv (the T2 renderers'
  stable entry) resolves to: it drains the claim engine one candidate at a time,
  spawning one ``cairn run`` child per claim, and never lets one failed or hazardous
  candidate stop the drain (TRIGGERS-PLAN.md §2).

stdlib + pyyaml only; no hidden clock, no network, no resident process.
"""

from __future__ import annotations

import errno
import fnmatch
import os
import plistlib
import re
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn, TextIO

import yaml

from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.proc import Runner
from cairn.kernel.types import Finding

TRIGGERS_YAML = "triggers.yaml"

_TRIGGER_KEYS = frozenset({"pipeline", "watch", "param", "glob", "on_done"})
_ON_DONE_VALUES = frozenset({"done", "delete"})

# A trigger name becomes a launchd job label segment and a systemd unit filename stem
# (trigger_launchd_label / trigger_systemd_unit_names) — both are structural identifiers,
# not free text, so the charset is a strict slug: non-empty, no leading separator, no
# path/section/shell metacharacters. This also rules out a name that could break a
# rendered systemd unit's line-oriented [Section] syntax (review-T2-quality-r1.md
# Finding 1), though render_trigger_systemd/_launchd keep their own defensive check
# below since workspace_dir/cairn_bin never pass through this validation (they're CLI
# args, not triggers.yaml fields).
_TRIGGER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# Characters that would break a rendered unit file's line-oriented syntax (a systemd
# unit is line-oriented INI; a literal newline mid-value ends the current Key=Value line
# and can open a new [Section]) if they ever reached render_trigger_systemd/_launchd
# uncaught.
_CONTROL_CHARS = ("\n", "\r", "\0")


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
    if not _TRIGGER_NAME_RE.fullmatch(name):
        _fail(
            f"trigger name {name!r} must be a non-empty slug matching "
            f"{_TRIGGER_NAME_RE.pattern!r} (it becomes a launchd job label and a systemd "
            "unit filename stem — see trigger_launchd_label/trigger_systemd_unit_names)",
            file,
        )
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
    """Reject a ``watch:`` that is absolute, that escapes the workspace via ``..``, or
    that carries a control character — a newline in ``watch`` survives into
    ``render_trigger_systemd``'s ``DirectoryNotEmpty=`` line unless caught here
    (review-T2-quality-r1.md Finding 1)."""
    if any(c in watch for c in _CONTROL_CHARS):
        _fail(
            f"trigger {name!r}: 'watch' must not contain a control character "
            f"(newline/CR/NUL): {watch!r}",
            file,
        )
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
    # missing_ok: a losing racer that hit the FileExistsError branch above may have
    # already unlinked candidate (it links to the same inode we just landed at dest) —
    # the postcondition (dest valid, candidate gone) holds regardless of which of the
    # two racers physically performs this unlink (G1).
    candidate.unlink(missing_ok=True)
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


# --------------------------------------------------------------------------- #
# Backend rendering — RENDER-ONLY, fully offline (TRIGGERS-PLAN.md §3)
# --------------------------------------------------------------------------- #


def _trigger_argv(trigger: Trigger, workspace_dir: Path, cairn_bin: str) -> list[str]:
    """The one stable entry every backend fires: never the expanded per-file work — a
    host watcher only ever knows a directory changed, so the argv can't and shouldn't
    encode which file. This also means editing ``triggers.yaml`` (pipeline, param, glob,
    on_done) changes behavior without re-sync, the same property SCHEDULING.md documents
    for ``cairn schedule run <name>``."""
    return [cairn_bin, "trigger", "run", trigger.name, "--workspace", str(workspace_dir)]


def _assert_render_safe(what: str, value: str) -> None:
    """Last line of defense before any value is interpolated into rendered unit text.

    ``trigger.name``/``watch`` are already validated at load time
    (``_parse_trigger``/``_validate_watch``), but ``workspace_dir`` and ``cairn_bin`` are
    CLI arguments that never pass through ``load_triggers`` — load-time validation cannot
    cover them, so this belt is what actually closes the section-injection vector a
    control character in any rendered value opens (review-T2-quality-r1.md Finding 1:
    a literal ``\\n`` in ``workspace_dir`` alone reaches the ``.service`` unit's
    ``WorkingDirectory=`` line ahead of, and independently of, the already-quoted
    ``ExecStart=``). Called for both backends — launchd's plistlib XML-escapes on its own,
    but the guarantee is made explicit here rather than left implicit in a third-party
    serializer's behavior.
    """
    if any(c in value for c in _CONTROL_CHARS):
        message = (
            f"cannot render trigger unit: {what} must not contain a control character "
            f"(newline/CR/NUL): {value!r}"
        )
        raise ConfigError(message, findings=[Finding("error", message)])


def trigger_launchd_label(name: str) -> str:
    """The launchd job label for a trigger (also the plist basename stem).

    The literal ``trigger.`` segment — vs schedkit's :func:`launchd_label`'s bare
    ``io.cairn.<name>`` — means a trigger and a schedule sharing one name can never
    collide as ~/Library/LaunchAgents plist labels.
    """
    return f"io.cairn.trigger.{name}"


def render_trigger_launchd(trigger: Trigger, workspace_dir: Path, cairn_bin: str) -> str:
    """Render one trigger as a launchd LaunchAgent plist (XML string) with a ``WatchPaths``
    key, plistlib-generated (never hand-concatenated XML — malformed XML is a silently
    dead LaunchAgent, not a load-time error a human sees).

    ``ThrottleInterval: 10`` — WatchPaths fires on EVERY mutation inside the watched dir,
    including the ``.claim/``/``.done/``/``.failed/`` renames ``cairn trigger run`` itself
    performs while draining the inbox (TRIGGERS-PLAN.md §3). Without a floor, one real
    event can cascade into a burst of self-triggered re-fires; ``scan_candidates`` always
    excludes those dot-dirs, so each extra firing just scans an already-empty top level —
    a cheap no-op — which is what makes throttling to once per 10s safe rather than lossy.
    """
    workspace_dir = Path(workspace_dir)
    watch_abs = watch_dir(trigger, workspace_dir)
    for what, value in (
        ("trigger name", trigger.name),
        ("workspace_dir", str(workspace_dir)),
        ("watch directory", str(watch_abs)),
        ("cairn_bin", cairn_bin),
    ):
        _assert_render_safe(what, value)
    plist: dict[str, Any] = {
        "Label": trigger_launchd_label(trigger.name),
        "ProgramArguments": _trigger_argv(trigger, workspace_dir, cairn_bin),
        "WatchPaths": [str(watch_abs)],
        "ThrottleInterval": 10,
    }
    return plistlib.dumps(plist, sort_keys=False).decode("utf-8")


def trigger_systemd_unit_names(name: str) -> tuple[str, str]:
    """The (``.path``, ``.service``) unit filenames for a trigger.

    The ``trigger-`` segment — vs schedkit's :func:`systemd_unit_names`'s bare
    ``cairn-<name>.service``/``.timer`` — means a trigger and a schedule sharing one name
    install as distinct unit files in the same systemd user directory rather than one
    silently overwriting the other's ``.service``.
    """
    return (f"cairn-trigger-{name}.path", f"cairn-trigger-{name}.service")


def render_trigger_systemd(
    trigger: Trigger, workspace_dir: Path, cairn_bin: str
) -> tuple[str, str]:
    """Render one trigger as a systemd (``.path``, ``.service``) unit pair (text strings).

    ``DirectoryNotEmpty=<abs watch dir>`` keeps re-firing while files remain in the watch
    dir — the drain-the-inbox semantics TRIGGERS-PLAN.md §3 wants, unlike a one-shot
    edge-triggered watch. Unlike ``render_trigger_launchd``'s ``ThrottleInterval``, no
    analogous re-fire floor is set here: the repeated firing while the watch dir stays
    non-empty IS the wanted drain behavior, not self-triggered noise to suppress, so
    throttling it would fight the design rather than protect it. ``ExecStart`` is the
    argv joined with shlex-style quoting (as
    schedkit's host-command line building does for cron) because systemd word-splits
    ``ExecStart=`` like a shell command line: an unquoted workspace path containing a
    space would otherwise silently truncate the argv. ``[Install] WantedBy=default.target``
    sits on the ``.path`` unit only — mirroring schedkit's asymmetry, where the
    activation unit (there, the ``.timer``) carries ``[Install]`` and the oneshot
    ``.service`` it triggers does not.
    """
    workspace_dir = Path(workspace_dir)
    watch_abs = watch_dir(trigger, workspace_dir)
    for what, value in (
        ("trigger name", trigger.name),
        ("workspace_dir", str(workspace_dir)),
        ("watch directory", str(watch_abs)),
        ("cairn_bin", cairn_bin),
    ):
        _assert_render_safe(what, value)
    _, service_name = trigger_systemd_unit_names(trigger.name)
    argv = _trigger_argv(trigger, workspace_dir, cairn_bin)
    path_unit = (
        "[Unit]\n"
        f"Description=cairn trigger watch: {trigger.name}\n\n"
        "[Path]\n"
        f"DirectoryNotEmpty={watch_abs}\n"
        f"Unit={service_name}\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    service_unit = (
        "[Unit]\n"
        f"Description=cairn trigger: {trigger.name}\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={workspace_dir}\n"
        f"ExecStart={shlex.join(argv)}\n"
    )
    return path_unit, service_unit


# --------------------------------------------------------------------------- #
# Effectful verbs — every side effect is dependency-injected (TRIGGERS-PLAN.md §3)
# --------------------------------------------------------------------------- #


def _require_dir(value: Path | None, backend: str, param: str) -> Path:
    """Mirrors schedkit's ``_require_dir`` exactly: an explicit target dir is mandatory
    for launchd/systemd (there is no default ``~/Library`` this module will ever guess
    at), and is created on demand so a first-ever sync doesn't need the caller to
    pre-`mkdir` it."""
    if value is None:
        raise ConfigError(
            f"{backend} backend requires an explicit {param} (the target directory)",
            findings=[Finding("error", f"{backend}: missing {param}")],
        )
    path = Path(value)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bad_backend(backend: str) -> NoReturn:
    """Mirrors schedkit's ``_bad_backend`` exactly."""
    raise ConfigError(
        f"unknown backend {backend!r} (expected cron, launchd, or systemd)",
        findings=[Finding("error", f"unknown backend {backend!r}")],
    )


def _cron_unsupported() -> NoReturn:
    """The cron backend has no file-watch facility (TRIGGERS-PLAN.md §3) — refuse with
    the documented fallback rather than fake a poll loop here. Shared by every effectful
    verb so `sync`/`list`/`remove --backend cron` all fail the same loud, explanatory way
    instead of `sync` refusing while `list`/`remove` silently report nothing installed.
    """
    message = (
        "the cron backend has no file-watch facility and cannot host a trigger "
        "(TRIGGERS-PLAN.md §3). Use the documented fallback instead: add a "
        "schedules.yaml entry that polls the inbox, e.g.\n"
        "  poll-<name>:\n"
        '    cron: "*/5 * * * *"\n'
        '    run: ["trigger", "run", "<name>", "--headless"]\n'
        "and `cairn schedule install --backend cron` it — idempotent and cheap on an "
        "empty inbox, so a 5-minute poll costs nothing when there is nothing to do."
    )
    raise ConfigError(message, findings=[Finding("error", "cron backend not supported for triggers")])


def _classify_plist(path: Path) -> tuple[str, str | None]:
    """Classify an existing plist sitting at a trigger's managed stem, from its OWN
    rendered content — never trust the filename alone (spec finding F1 / addendum 2).
    Returns ``("trigger", name)`` when ``ProgramArguments`` has the exact
    ``[cairn_bin, "trigger", "run", name, ...]`` shape :func:`_trigger_argv` renders
    (this IS our prior render, for that trigger); ``("schedule", None)`` when it instead
    has schedkit's ``[cairn_bin, "schedule", "run", ...]`` shape; ``("unmanaged", None)``
    for anything else (foreign content, an unparseable/corrupted plist, or a shape that
    matches neither convention).
    """
    try:
        doc = plistlib.loads(path.read_bytes())
    except Exception:
        return "unmanaged", None
    argv = doc.get("ProgramArguments")
    if not isinstance(argv, list) or len(argv) < 4 or not all(isinstance(a, str) for a in argv):
        return "unmanaged", None
    if argv[1:3] == ["trigger", "run"]:
        return "trigger", argv[3]
    if len(argv) >= 3 and argv[1:2] == ["schedule"]:
        return "schedule", None
    return "unmanaged", None


def _classify_systemd_service(path: Path) -> tuple[str, str | None]:
    """The ``.service``-file counterpart of :func:`_classify_plist`: reads back the
    ``ExecStart=`` line (shlex-split the same way systemd itself word-splits it) rather
    than trusting the filename.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # A non-UTF-8 file at this stem is exactly the "foreign content" this
        # function's own contract promises to shrug off as unmanaged (same posture as
        # _classify_plist's bare `except Exception`) — `Path.read_text` raises
        # UnicodeDecodeError (a ValueError subclass, NOT an OSError subclass) on
        # non-UTF-8 bytes, and this classifier is called across every
        # cairn-trigger-*.service in the target dir by _sync_systemd's prune sweep and
        # _installed_trigger_names — one stray binary file anywhere in that shared
        # directory must never crash the sweep for every trigger (review-T3-quality-r1.md
        # Finding 1).
        return "unmanaged", None
    exec_line = next((line for line in text.splitlines() if line.startswith("ExecStart=")), None)
    if exec_line is None:
        return "unmanaged", None
    try:
        argv = shlex.split(exec_line[len("ExecStart=") :])
    except ValueError:
        return "unmanaged", None
    if len(argv) >= 4 and argv[1:3] == ["trigger", "run"]:
        return "trigger", argv[3]
    if len(argv) >= 3 and argv[1:2] == ["schedule"]:
        return "schedule", None
    return "unmanaged", None


def _classify_systemd_path_unit(path: Path) -> tuple[str, str | None]:
    """The ``.path``-file counterpart of :func:`_classify_plist`. Only triggerkit ever
    renders a ``.path`` unit — schedkit's systemd backend has no analogue — so this can
    never collide with a *schedule's* file the way ``.service`` can; an operator hand-
    placing an unrelated file at the managed stem is still possible, so content is
    checked all the same rather than trusting presence alone. Reads the ``Unit=`` line
    and recovers the trigger name from the ``cairn-trigger-<name>.service`` target it
    names.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Same posture as _classify_systemd_service (see its comment) — non-UTF-8
        # content at this stem is foreign content, not a crash (review-T3-quality-r1.md
        # Finding 1).
        return "unmanaged", None
    unit_line = next((line for line in text.splitlines() if line.startswith("Unit=")), None)
    if unit_line is None:
        return "unmanaged", None
    match = re.fullmatch(r"cairn-trigger-(.+)\.service", unit_line[len("Unit=") :].strip())
    if not match:
        return "unmanaged", None
    return "trigger", match.group(1)


def _require_ownership(path: Path, trigger_name: str, classify: Callable[[Path], tuple[str, str | None]]) -> None:
    """Refuse to write or prune ``path`` unless it is either absent or recognizably OUR
    prior render for THIS trigger (spec finding F1, addendum 2 — a hard requirement,
    not a heuristic nicety): a schedule named ``trigger-X`` and a trigger named ``X``
    both render ``cairn-trigger-X.service``, and the T2 stem/label namespace alone
    cannot make this impossible, so ownership is verified from the existing file's
    content (via ``classify``) before it is ever touched, whether by a write in
    `sync_triggers` or a delete in `remove_trigger`/`sync_triggers`'s prune pass.
    """
    if not path.is_file():
        return
    owner_kind, owner_name = classify(path)
    if owner_kind == "trigger" and owner_name == trigger_name:
        return
    likely_owner = "a schedule" if owner_kind == "schedule" else "an unmanaged file"
    message = (
        f"refusing to write trigger {trigger_name!r}'s unit file {path}: it already "
        f"exists and is not recognizably managed by this trigger — it looks like "
        f"{likely_owner}. Sync never overwrites a file it doesn't own; rename or remove "
        "it by hand if it is stale (TRIGGERS-PLAN.md §3, spec finding F1)."
    )
    raise ConfigError(message, findings=[Finding("error", message)], file=str(path))


@dataclass(frozen=True)
class TriggerStatus:
    """One trigger's status as ``trigger list`` reports it (TRIGGERS-PLAN.md §2/§3):
    declared in ``triggers.yaml``, installed on the host watcher, and any files sitting
    in ``.claim/`` from a crash mid-firing — surfaced here so the operator can re-drop or
    discard them; never auto-retried. ``stuck`` is only ever non-empty for a DECLARED
    trigger (its ``watch:`` must resolve to inspect ``.claim/``); a trigger that's
    installed but no longer declared has no ``watch:`` left to check.
    """

    name: str
    declared: bool
    installed: bool
    stuck: tuple[Path, ...] = ()


def sync_triggers(
    workspace_dir: Path,
    *,
    backend: str,
    runner: Runner,
    cairn_bin: str,
    launchd_dir: Path | None = None,
    systemd_dir: Path | None = None,
) -> list[str]:
    """Install/update every declared trigger into the host watcher and prune managed
    files whose trigger left ``triggers.yaml`` — mirrors schedkit's ``install`` verb's
    Runner-injection and target-dir shape, with two triggers-specific hardenings this
    module adds on top: a write or prune NEVER touches a file it can't attribute to
    itself (:func:`_require_ownership`, spec finding F1 / addendum 2), and a repeated
    sync of byte-identical rendered output issues ZERO host-watcher calls (unload/load,
    daemon-reload, enable) — a true no-op, not merely an unchanged file on disk.

    Returns the unit/plist filenames installed, updated, or pruned this call (a trigger
    that was already current is still included — it's part of what's installed after
    this call returns, even though nothing was written for it).
    """
    workspace_dir = Path(workspace_dir)
    triggers = load_triggers(workspace_dir)
    if backend == "cron":
        _cron_unsupported()
    if backend == "launchd":
        return _sync_launchd(triggers, workspace_dir, runner, cairn_bin, launchd_dir)
    if backend == "systemd":
        return _sync_systemd(triggers, workspace_dir, runner, cairn_bin, systemd_dir)
    _bad_backend(backend)


def _sync_launchd(
    triggers: dict[str, Trigger],
    workspace_dir: Path,
    runner: Runner,
    cairn_bin: str,
    launchd_dir: Path | None,
) -> list[str]:
    target = _require_dir(launchd_dir, "launchd", "launchd_dir")
    # Pre-pass: validate ownership for EVERY declared trigger before writing ANY of
    # them, so an ownership refusal for trigger N never leaves an earlier trigger's
    # freshly-written plist on disk from this same call (review-T3-quality-r1.md
    # Finding 3 — same "check everything, then act" pattern remove_trigger's systemd
    # branch now uses for its own two-stem check).
    for trigger in triggers.values():
        filename = f"{trigger_launchd_label(trigger.name)}.plist"
        _require_ownership(target / filename, trigger.name, _classify_plist)
    touched: list[str] = []
    for trigger in triggers.values():
        filename = f"{trigger_launchd_label(trigger.name)}.plist"
        path = target / filename
        rendered = render_trigger_launchd(trigger, workspace_dir, cairn_bin)
        if path.is_file() and path.read_text(encoding="utf-8") == rendered:
            touched.append(filename)  # already current — zero host-watcher calls
            continue
        path.write_text(rendered, encoding="utf-8")
        runner.run(["launchctl", "unload", str(path)])  # idempotent reload, mirrors schedkit.install
        runner.run(["launchctl", "load", str(path)])
        touched.append(filename)
    for path in sorted(target.glob("io.cairn.trigger.*.plist")):
        owner_kind, owner_name = _classify_plist(path)
        if owner_kind != "trigger" or owner_name in triggers:
            continue  # not ours, or its trigger is still declared — never touch
        runner.run(["launchctl", "unload", str(path)])
        path.unlink()
        touched.append(path.name)
    return touched


def _sync_systemd(
    triggers: dict[str, Trigger],
    workspace_dir: Path,
    runner: Runner,
    cairn_bin: str,
    systemd_dir: Path | None,
) -> list[str]:
    target = _require_dir(systemd_dir, "systemd", "systemd_dir")
    # Pre-pass: validate ownership of BOTH stems for EVERY declared trigger before
    # writing ANY of them. Writes happen per-trigger inside the loop below but
    # daemon-reload/enable are deferred until after the whole loop finishes — an
    # ownership refusal raised mid-loop (a T3-only addition unrelated to disk I/O, so
    # far more reachable than a raw write failure) would otherwise leave earlier
    # triggers' unit files written to disk but never reloaded into systemd: a silent,
    # undetectable-from-the-return-value inconsistency between disk and the live host
    # scheduler (review-T3-quality-r1.md Finding 3). Validating everything up front
    # keeps a refusal a true no-op: the target dir stays byte-for-byte untouched.
    for trigger in triggers.values():
        path_name, service_name = trigger_systemd_unit_names(trigger.name)
        _require_ownership(target / path_name, trigger.name, _classify_systemd_path_unit)
        _require_ownership(target / service_name, trigger.name, _classify_systemd_service)

    touched: list[str] = []
    to_enable: list[str] = []
    needs_reload = False
    for trigger in triggers.values():
        path_name, service_name = trigger_systemd_unit_names(trigger.name)
        path_unit_path = target / path_name
        service_unit_path = target / service_name
        path_text, service_text = render_trigger_systemd(trigger, workspace_dir, cairn_bin)
        unchanged = (
            path_unit_path.is_file()
            and path_unit_path.read_text(encoding="utf-8") == path_text
            and service_unit_path.is_file()
            and service_unit_path.read_text(encoding="utf-8") == service_text
        )
        touched.extend([path_name, service_name])
        if unchanged:
            continue  # already current — zero host-watcher calls for this trigger
        path_unit_path.write_text(path_text, encoding="utf-8")
        service_unit_path.write_text(service_text, encoding="utf-8")
        needs_reload = True
        to_enable.append(path_name)
    # Prune managed files whose trigger left triggers.yaml. `.path` and `.service` are
    # swept independently (not derived from each other) so an orphaned half-pair — e.g.
    # a `.service` deleted by hand while its `.path` survives — is still caught.
    for path in sorted(target.glob("cairn-trigger-*.path")):
        owner_kind, owner_name = _classify_systemd_path_unit(path)
        if owner_kind != "trigger" or owner_name in triggers:
            continue
        runner.run(["systemctl", "--user", "disable", "--now", path.name])
        path.unlink()
        touched.append(path.name)
        needs_reload = True
    for path in sorted(target.glob("cairn-trigger-*.service")):
        owner_kind, owner_name = _classify_systemd_service(path)
        if owner_kind != "trigger" or owner_name in triggers:
            continue
        path.unlink()
        touched.append(path.name)
        needs_reload = True
    if needs_reload:
        runner.run(["systemctl", "--user", "daemon-reload"])
    for path_name in to_enable:
        runner.run(["systemctl", "--user", "enable", "--now", path_name])
    return touched


def remove_trigger(
    name: str,
    workspace_dir: Path,
    *,
    backend: str,
    runner: Runner,
    launchd_dir: Path | None = None,
    systemd_dir: Path | None = None,
) -> bool:
    """Remove one trigger's managed unit/plist file(s) from the host watcher.

    Returns whether anything was actually removed — ``False`` when nothing was
    installed under this name (idempotent, mirrors schedkit's ``uninstall`` tolerating
    a re-run). Applies the same ownership guard `sync_triggers` does
    (:func:`_require_ownership`): a file sitting at this trigger's managed stem that
    isn't recognizably OUR prior render for ``name`` is left untouched and raises loud,
    rather than silently deleting a schedule's or an operator's file that happens to
    share the stem.
    """
    workspace_dir = Path(workspace_dir)
    if backend == "cron":
        _cron_unsupported()
    if backend == "launchd":
        target = _require_dir(launchd_dir, backend, "launchd_dir")
        path = target / f"{trigger_launchd_label(name)}.plist"
        if not path.is_file():
            return False
        _require_ownership(path, name, _classify_plist)
        runner.run(["launchctl", "unload", str(path)])
        path.unlink()
        return True
    if backend == "systemd":
        target = _require_dir(systemd_dir, backend, "systemd_dir")
        path_name, service_name = trigger_systemd_unit_names(name)
        path_unit_path = target / path_name
        service_unit_path = target / service_name
        # Validate ownership of BOTH stems before acting on EITHER (mirrors
        # _sync_systemd's write path) — checking .service only after already disabling
        # and unlinking .path means an ownership refusal on .service (e.g. a
        # same-named schedule's live unit file) leaves a real .path unit destructively
        # removed behind what looks to the caller like a clean, no-op refusal
        # (review-T3-quality-r1.md Finding 2).
        if path_unit_path.is_file():
            _require_ownership(path_unit_path, name, _classify_systemd_path_unit)
        if service_unit_path.is_file():
            _require_ownership(service_unit_path, name, _classify_systemd_service)
        removed = False
        if path_unit_path.is_file():
            runner.run(["systemctl", "--user", "disable", "--now", path_name])
            path_unit_path.unlink()
            removed = True
        if service_unit_path.is_file():
            service_unit_path.unlink()
            removed = True
        if removed:
            runner.run(["systemctl", "--user", "daemon-reload"])
        return removed
    _bad_backend(backend)


def _installed_trigger_names(
    backend: str, *, launchd_dir: Path | None, systemd_dir: Path | None
) -> set[str]:
    """The set of trigger names with a recognizably trigger-owned unit/plist file on the
    host, read back straight from the target dir — no `runner` needed for launchd/
    systemd (mirrors schedkit's `list_installed`, which likewise only actually invokes
    its runner for the cron branch).
    """
    if backend == "launchd":
        target = _require_dir(launchd_dir, backend, "launchd_dir")
        names: set[str] = set()
        for path in target.glob("io.cairn.trigger.*.plist"):
            owner_kind, owner_name = _classify_plist(path)
            if owner_kind == "trigger" and owner_name is not None:
                names.add(owner_name)
        return names
    if backend == "systemd":
        target = _require_dir(systemd_dir, backend, "systemd_dir")
        names = set()
        for path in target.glob("cairn-trigger-*.service"):
            owner_kind, owner_name = _classify_systemd_service(path)
            if owner_kind == "trigger" and owner_name is not None:
                names.add(owner_name)
        return names
    if backend == "cron":
        _cron_unsupported()
    _bad_backend(backend)


def list_installed_triggers(
    workspace_dir: Path,
    *,
    backend: str,
    runner: Runner,
    launchd_dir: Path | None = None,
    systemd_dir: Path | None = None,
) -> list[TriggerStatus]:
    """Declared vs installed vs stuck, one :class:`TriggerStatus` per name in the union
    of ``triggers.yaml`` and what's actually on the host (TRIGGERS-PLAN.md §2/§3).

    ``runner`` is accepted (never optional) for interface symmetry with schedkit's
    ``list_installed`` and to keep the door open for a future cron-status read; the
    launchd/systemd branches read the host state from the target directories directly
    and never invoke it, exactly as schedkit's own ``list_installed`` only invokes ITS
    runner for the cron branch.
    """
    workspace_dir = Path(workspace_dir)
    triggers = load_triggers(workspace_dir)
    installed_names = _installed_trigger_names(backend, launchd_dir=launchd_dir, systemd_dir=systemd_dir)
    statuses: list[TriggerStatus] = []
    for name in sorted(set(triggers) | installed_names):
        trigger = triggers.get(name)
        stuck: tuple[Path, ...] = ()
        if trigger is not None:
            stuck = tuple(stuck_claims(watch_dir(trigger, workspace_dir)))
        statuses.append(
            TriggerStatus(
                name=name,
                declared=name in triggers,
                installed=name in installed_names,
                stuck=stuck,
            )
        )
    return statuses


def _run_one(
    trigger: Trigger,
    claimed_path: Path,
    workspace_dir: Path,
    runner: Runner,
    cairn_bin: str,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> bool:
    """Fire the one child ``cairn run`` for a single claimed event (TRIGGERS-PLAN.md §2
    step 3). ``claimed_path`` is ALWAYS the exact path :func:`claim` returned — including
    any ``-v2`` collision suffix — never reconstructed from the original candidate's
    basename (T1 quality finding G2 / addendum): the child must receive the ledger's
    real location, not a guess at what it might be. Already absolute (built from
    :func:`watch_dir`'s resolved watch dir), so no further resolution is applied here —
    doing so would risk dereferencing a claimed file that is itself a symlink, which T1's
    claim/consume deliberately never do.

    Mirrors ``schedkit.run_schedule``'s re-emission exactly: the Runner captures the
    child's stdout/stderr, so a firing that halts (a halt reason, a resume hint) would
    otherwise produce ZERO output and a launchd/systemd-fired trigger's operator would
    see nothing but an exit code — silently rotting, which §4 forbids. When ``out``/
    ``err`` are provided, the captured streams are re-emitted VERBATIM to them after the
    child completes; when they are None, nothing is re-emitted (matches ``run_schedule``'s
    backward-compatible default).
    """
    argv = [
        cairn_bin,
        "run",
        trigger.pipeline,
        "--headless",
        "--param",
        f"{trigger.param}={claimed_path}",
    ]
    result = runner.run(argv, cwd=workspace_dir)
    if out is not None and result.stdout:
        out.write(result.stdout)
    if err is not None and result.stderr:
        err.write(result.stderr)
    return result.returncode == 0


def run_trigger(
    name: str,
    workspace_dir: Path,
    *,
    runner: Runner,
    cairn_bin: str,
    now: datetime,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Drain trigger ``name``'s inbox: scan, claim each candidate, fire one ``cairn run``
    child per claim, consume on outcome (TRIGGERS-PLAN.md §2). This is the function the
    host watcher's ``cairn trigger run <name>`` argv (the T2 renderers' stable entry)
    resolves to.

    ``now`` is accepted per the module's no-hidden-clock discipline (mirroring
    schedkit's ``install``/``run_schedule`` posture) even though nothing in this
    function currently branches on it — no timestamp is embedded at this layer (the
    child's own ``run_id`` templating does that, per TRIGGERS-PLAN.md §2's closing
    paragraph). Kept as an injected parameter rather than a bare ``datetime.now()`` so a
    future addition (e.g. stuck-claim age reporting) never needs a signature change.

    ``out``/``err`` mirror ``schedkit.run_schedule`` exactly (same signature, same
    backward-compatible None default): the Runner captures each child's stdout/stderr,
    so a firing that halts would otherwise produce ZERO output on the operator's
    notification channel — the exact "silently rotting" failure run_schedule exists to
    prevent, and this is the doctrine's primary target platform (launchd/systemd-fired).
    When given, every child's captured streams are re-emitted verbatim to them via
    :func:`_run_one` as each candidate is processed. A claim/spawn/consume hazard (see
    below) has no child stream to re-emit — instead, a one-line diagnostic naming the
    candidate and the exception is written to ``err`` (falling back to ``sys.stderr``
    when ``err`` is None, so the hazard is never silent even when the caller passes
    nothing).

    Exit code: ``0`` when every candidate was processed clean, OR there was nothing to
    claim (an empty scan is a successful no-op drain, not a failure); nonzero when ANY
    candidate failed to process — its child exited nonzero, OR the claim/spawn/consume
    step itself hazarded (a raised exception, not just a nonzero child exit). A failing
    candidate never stops the drain — every remaining candidate still gets claimed and
    run (the brief's rejected alternative is retrying a failed event, not draining past
    it: draining past it is required). The failed
    claim moves to ``.failed/`` (never auto-retried) while the run overall reports
    failure via its exit code.

    :func:`claim` can raise :class:`CairnError` for a filesystem/platform hazard (a
    hardlink-unsupported platform, or ``.claim/`` on a different filesystem than the
    watch dir — T1's ``_hardlink``) instead of returning ``None``/a path. That hazard
    afflicts every candidate in this watch dir identically, not one poison file, but the
    same principle applies: a clear halt of that ONE event beats a crash of the whole
    drain. It is caught per-candidate, counted as a failure, and the loop moves on to
    the next candidate rather than aborting the whole run. Nothing was ever claimed in
    that case, so there is no claim path to :func:`consume` — the candidate is left
    exactly where it was, to be picked up again on the NEXT firing once the underlying
    filesystem/platform hazard is fixed (a structural misconfiguration, not the
    poison-file scenario ``.failed/``-and-stop targets).

    Once a candidate IS claimed, the child spawn (:func:`_run_one`) and :func:`consume`
    are wrapped in that SAME per-candidate isolation, not just ``claim()`` — a runner
    that can't even spawn the child (a missing ``cairn_bin``, a ``PermissionError``) or a
    ``consume`` that hazards while retiring the claim (its own ``_hardlink`` path can
    raise, per T1's docstring) must not abort the whole drain either
    (review-T3-quality-r1.md Finding 4). Unlike the pre-claim hazard above, this
    candidate's file IS now sitting in ``.claim/`` with no recorded outcome once such an
    exception hits — by definition a stuck claim (never auto-retried; surfaced via
    :func:`stuck_claims`/``trigger list`` for the operator to re-drop or discard by
    hand), a deliberate choice rather than a bug: a claim whose child never ran has no
    known outcome to consume it with, so surfacing it as stuck is honest, where silently
    retrying it would risk re-running a child that already did partial, unknown work.
    """
    workspace_dir = Path(workspace_dir)
    triggers = load_triggers(workspace_dir)
    if name not in triggers:
        raise ConfigError(
            f"no trigger named {name!r} in triggers.yaml "
            f"(declared: {', '.join(sorted(triggers)) or '(none)'})",
            findings=[Finding("error", f"unknown trigger {name!r}")],
        )
    trigger = triggers[name]
    watch_abs = watch_dir(trigger, workspace_dir)
    diag = err if err is not None else sys.stderr
    any_failed = False
    for candidate in scan_candidates(watch_abs, trigger.glob):
        try:
            claimed = claim(watch_abs, candidate)
        except CairnError as exc:
            any_failed = True
            print(
                f"cairn: trigger {name!r}: candidate {candidate.name!r} hazarded and was "
                f"left in place: {exc}",
                file=diag,
            )
            continue
        if claimed is None:
            continue  # lost the claim race to a concurrent firing — not our event
        try:
            ok = _run_one(trigger, claimed, workspace_dir, runner, cairn_bin, out=out, err=err)
            consume(watch_abs, claimed, ok=ok, on_done=trigger.on_done)
        except Exception as exc:
            # The child spawn or the consume step itself hazarded (see the docstring's
            # "Once a candidate IS claimed" paragraph) — this candidate is now a stuck
            # claim by definition, not silently lost or retried; count it as a failure
            # and move on rather than aborting the whole drain (review-T3-quality-r1.md
            # Finding 4).
            any_failed = True
            print(
                f"cairn: trigger {name!r}: candidate {candidate.name!r} hazarded and was "
                f"left in .claim/ as a stuck claim: {exc}",
                file=diag,
            )
            continue
        if not ok:
            any_failed = True
    return 1 if any_failed else 0
