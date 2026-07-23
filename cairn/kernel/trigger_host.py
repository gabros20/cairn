"""trigger_host — trigger declaration + host-watcher integration.

Parse/validate triggers.yaml, render launchd/systemd units, and the effectful
sync/remove/list verbs. Middle layer of the triggerkit split (docs/FACTORY-PLAN.md
§3 W0.5 / D10): may import queue_ledger (stuck_claims for list); must not import
queue_drain.
"""

from __future__ import annotations

import plistlib
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import yaml

from cairn.kernel.errors import ConfigError
from cairn.kernel.proc import Runner
from cairn.kernel.queue_ledger import count_by_class, ledger_counts, stuck_claims
from cairn.kernel.types import Finding

TRIGGERS_YAML = "triggers.yaml"

_TRIGGER_KEYS = frozenset({
    "pipeline",
    "watch",
    "param",
    "glob",
    "on_done",
    # W3 optional back-pressure / admission keys (absent = today's serial unbounded drain)
    "concurrency",
    "order",
    "waiting_max",
    "blocked_max",
    "capacity_max",
    "wip_max",
    "inbox_max",
    # W3 identity (T1): default off = arbitrary names, no reservation/dedupe/defer
    "identity",
    "max_item_bytes",
})
_ON_DONE_VALUES = frozenset({"done", "delete"})
_ORDER_VALUES = frozenset({"name", "aged"})
_IDENTITY_VALUES = frozenset({"off", "strict"})

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
    relative); resolve it with :func:`watch_dir`.

    Optional W3 admission / back-pressure keys (all optional; absent = today's behavior —
    serial drain, no caps, name order). Example ``triggers.yaml`` fragment::

        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies/
          # concurrency: 1          # max children at once (default 1 = serial; >1 = pool)
          # order: name             # "name" lexicographic (default) | "aged" priority aging
          # waiting_max: 5          # stop admitting when needs-human depth reaches this
          # blocked_max: 5          # stop on blocked depth (default = waiting_max when set)
          # capacity_max: 10        # stop on capacity-park depth
          # wip_max: 20             # stop when inflight (claimed + all waiting) reaches this
          # inbox_max: 50           # spool cap for pullers (W4); list-only here, not an admit gate
          # identity: off           # "off" (default) | "strict" — filename grammar +
          #                         # reservation/dedupe/defer (FACTORY-PLAN T1)
          # max_item_bytes: 1048576 # admission envelope byte cap (strict only; default 1 MiB)

    Soft vs hard caps under ``concurrency > 1``: ``waiting_max`` / ``blocked_max`` /
    ``capacity_max`` are soft — up to ``concurrency`` in-flight items may land in
    one class past its cap before the next admission check observes them (outcome
    class is unknown until a child retires). ``wip_max`` is hard (claim updates
    ``.claim/`` synchronously on the admitter). Bounded overshoot, not unbounded
    (FACTORY-PLAN §2 T2 tradeoff, pool-width bound).

    ``identity: off`` (default) keeps arbitrary inbox names and no reservation —
    byte-identical to pre-T12. ``identity: strict`` enables the T1 envelope.
    """

    name: str
    pipeline: str
    watch: str
    param: str = "event"
    glob: str = "*"
    on_done: str = "done"  # "done" | "delete"
    concurrency: int = 1
    order: str = "name"  # "name" | "aged"
    waiting_max: int | None = None
    blocked_max: int | None = None
    capacity_max: int | None = None
    wip_max: int | None = None
    inbox_max: int | None = None
    identity: str = "off"  # "off" | "strict"
    max_item_bytes: int | None = None  # None → DEFAULT_MAX_ITEM_BYTES when strict


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

    concurrency = 1
    if "concurrency" in entry:
        concurrency = _parse_positive_int(name, "concurrency", entry["concurrency"], file)

    order = entry.get("order", "name")
    if order not in _ORDER_VALUES:
        _fail(
            f"trigger {name!r}: 'order' must be one of "
            f"{sorted(_ORDER_VALUES)}, got {order!r}",
            file,
        )

    waiting_max = _parse_optional_positive_int(name, "waiting_max", entry, file)
    blocked_max = _parse_optional_positive_int(name, "blocked_max", entry, file)
    # Default blocked_max = waiting_max when only the judgment-lane cap is authored.
    if blocked_max is None and waiting_max is not None:
        blocked_max = waiting_max
    capacity_max = _parse_optional_positive_int(name, "capacity_max", entry, file)
    wip_max = _parse_optional_positive_int(name, "wip_max", entry, file)
    inbox_max = _parse_optional_positive_int(name, "inbox_max", entry, file)

    # YAML 1.1 treats bare `off`/`on` as booleans — coerce so `identity: off`
    # (the documented default spelling) is not a ConfigError.
    raw_identity = entry.get("identity", "off")
    if raw_identity is False:
        identity = "off"
    elif raw_identity is True:
        _fail(
            f"trigger {name!r}: 'identity' must be one of "
            f"{sorted(_IDENTITY_VALUES)}, got {raw_identity!r} "
            f"(YAML boolean — use the string 'off' or 'strict')",
            file,
        )
    else:
        identity = raw_identity
    if identity not in _IDENTITY_VALUES:
        _fail(
            f"trigger {name!r}: 'identity' must be one of "
            f"{sorted(_IDENTITY_VALUES)}, got {identity!r}",
            file,
        )

    max_item_bytes: int | None = None
    if "max_item_bytes" in entry:
        max_item_bytes = _parse_positive_int(
            name, "max_item_bytes", entry["max_item_bytes"], file
        )

    return Trigger(
        name=name,
        pipeline=pipeline,
        watch=watch,
        param=param,
        glob=glob,
        on_done=on_done,
        concurrency=concurrency,
        order=order,
        waiting_max=waiting_max,
        blocked_max=blocked_max,
        capacity_max=capacity_max,
        wip_max=wip_max,
        inbox_max=inbox_max,
        identity=identity,
        max_item_bytes=max_item_bytes,
    )


def _parse_positive_int(name: str, key: str, value: Any, file: Path) -> int:
    """Positive int (>0). Rejects bool (YAML ``true`` is an int subclass)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(
            f"trigger {name!r}: {key!r} must be a positive integer, got {value!r}",
            file,
        )
    return value


def _parse_optional_positive_int(
    name: str, key: str, entry: dict[str, Any], file: Path
) -> int | None:
    if key not in entry:
        return None
    return _parse_positive_int(name, key, entry[key], file)


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
    declared in ``triggers.yaml``, installed on the host watcher, ledger depths
    (waiting/failed/done + W3 class depths), and any files sitting in ``.claim/`` from a
    crash mid-firing — surfaced here so the operator can re-drop or discard them; never
    auto-retried. ``stuck`` and the depth counts are only ever non-empty for a DECLARED
    trigger (its ``watch:`` must resolve); a trigger that's installed but no longer
    declared has no ``watch:`` left to check.

    W3 additive fields (defaults 0 / None so older callers keep working):
    ``needs_human`` / ``blocked`` / ``capacity`` (waiting-class splits), ``inflight``
    (claimed + all waiting), ``spool`` (inbox candidates), plus the authored caps when
    the trigger is declared.
    """

    name: str
    declared: bool
    installed: bool
    stuck: tuple[Path, ...] = ()
    waiting: int = 0
    failed: int = 0
    done: int = 0
    needs_human: int = 0
    blocked: int = 0
    capacity: int = 0
    inflight: int = 0
    spool: int = 0
    concurrency: int = 1
    order: str = "name"
    waiting_max: int | None = None
    blocked_max: int | None = None
    capacity_max: int | None = None
    wip_max: int | None = None
    inbox_max: int | None = None


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
        waiting = failed = done = 0
        needs_human = blocked = capacity = inflight = spool = 0
        concurrency = 1
        order = "name"
        waiting_max = blocked_max = capacity_max = wip_max = inbox_max = None
        if trigger is not None:
            watch_abs = watch_dir(trigger, workspace_dir)
            stuck = tuple(stuck_claims(watch_abs))
            counts = ledger_counts(watch_abs)
            waiting = counts["waiting"]
            failed = counts["failed"]
            done = counts["done"]
            depths = count_by_class(watch_abs, glob=trigger.glob)
            needs_human = depths["needs_human"]
            blocked = depths["blocked"]
            capacity = depths["capacity"]
            inflight = depths["inflight"]
            spool = depths["spool"]
            concurrency = trigger.concurrency
            order = trigger.order
            waiting_max = trigger.waiting_max
            blocked_max = trigger.blocked_max
            capacity_max = trigger.capacity_max
            wip_max = trigger.wip_max
            inbox_max = trigger.inbox_max
        statuses.append(
            TriggerStatus(
                name=name,
                declared=name in triggers,
                installed=name in installed_names,
                stuck=stuck,
                waiting=waiting,
                failed=failed,
                done=done,
                needs_human=needs_human,
                blocked=blocked,
                capacity=capacity,
                inflight=inflight,
                spool=spool,
                concurrency=concurrency,
                order=order,
                waiting_max=waiting_max,
                blocked_max=blocked_max,
                capacity_max=capacity_max,
                wip_max=wip_max,
                inbox_max=inbox_max,
            )
        )
    return statuses
