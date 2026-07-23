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

from cairn.kernel.config import parse_duration
from cairn.kernel.errors import ConfigError
from cairn.kernel.proc import Runner
from cairn.kernel.queue_ledger import (
    LEASE_TTL_DEFAULT,
    LEASE_TTL_OFF,
    count_by_class,
    effective_lease_ttl,
    ledger_counts,
    lease_status,
    stamp_ledger_version,
    stuck_claims,
)
from cairn.kernel.types import Finding
from cairn.kernel.wsid import label_prefix_for, workspace_id, ws8

TRIGGERS_YAML = "triggers.yaml"

# Reconcile beat: host timer firing `cairn factory reconcile --workspace <ws>` (D1/D6).
# Name is a schedule-shaped stem under the ws-scoped label prefix; install/remove via
# schedkit (timers are schedkit's boundary; this module REQUESTS the beat).
RECONCILE_BEAT_NAME = "factory-reconcile"
RECONCILE_BEAT_CRON = "*/10 * * * *"  # every 10 minutes

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
    # W3 liveness (T13): claim leases — default ON only for concurrency>1
    "lease",
    # W5 autonomy lane: optional name → child `cairn run --lane <name>`
    "lane",
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
          # lease: off              # "off" | duration ("60m") — claim liveness (T13).
          #                         # Default ON (60m) only when concurrency > 1;
          #                         # serial default stays stuck-forever (D7).

    Soft vs hard caps under ``concurrency > 1``: ``waiting_max`` / ``blocked_max`` /
    ``capacity_max`` are soft — up to ``concurrency`` in-flight items may land in
    one class past its cap before the next admission check observes them (outcome
    class is unknown until a child retires). ``wip_max`` is hard (claim updates
    ``.claim/`` synchronously on the admitter). Bounded overshoot, not unbounded
    (FACTORY-PLAN §2 T2 tradeoff, pool-width bound).

    ``identity: off`` (default) keeps arbitrary inbox names and no reservation —
    byte-identical to pre-T12. ``identity: strict`` enables the T1 envelope.

    ``lease`` (T13): stored as seconds or the sentinels ``LEASE_TTL_DEFAULT`` /
    ``LEASE_TTL_OFF``. Resolve with :func:`cairn.kernel.queue_ledger.effective_lease_ttl`.

    ``lane`` (W5): optional autonomy-profile name. When set, the host-watcher argv
    and each child ``cairn run`` receive ``--lane <name>`` (the pipeline must declare
    that lane). Absent = today's behavior (no lane selection).
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
    # LEASE_TTL_DEFAULT (-1) = policy; LEASE_TTL_OFF (0) = off; >0 = explicit ttl.
    lease_ttl_s: int = LEASE_TTL_DEFAULT
    lane: str | None = None


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

    lease_ttl_s = _parse_lease(name, entry, file)

    lane: str | None = None
    if "lane" in entry:
        raw_lane = entry["lane"]
        if not isinstance(raw_lane, str) or not raw_lane.strip():
            _fail(
                f"trigger {name!r}: 'lane' must be a non-empty string, got {raw_lane!r}",
                file,
            )
        lane = raw_lane.strip()

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
        lease_ttl_s=lease_ttl_s,
        lane=lane,
    )


def _parse_lease(name: str, entry: dict[str, Any], file: Path) -> int:
    """Parse ``lease:`` → ttl seconds or LEASE_TTL_DEFAULT / LEASE_TTL_OFF.

    Accepts ``off`` (and YAML bool ``false``/``False``), a duration string
    (``60m``, ``1h`` — via :func:`parse_duration`), or a positive int (seconds).
    Absent key → :data:`LEASE_TTL_DEFAULT` (policy: on only for concurrency>1).
    """
    if "lease" not in entry:
        return LEASE_TTL_DEFAULT
    raw = entry["lease"]
    # YAML 1.1: bare `off` → False; bare `on` → True (reject True).
    if raw is False or raw == "off":
        return LEASE_TTL_OFF
    if raw is True or raw == "on":
        _fail(
            f"trigger {name!r}: 'lease' must be 'off' or a duration like '60m', "
            f"got {raw!r} (use an explicit duration to enable leases on a serial trigger)",
            file,
        )
    if isinstance(raw, int) and not isinstance(raw, bool):
        if raw < 1:
            _fail(
                f"trigger {name!r}: 'lease' duration must be a positive number of "
                f"seconds, got {raw!r}",
                file,
            )
        return raw
    if isinstance(raw, str):
        try:
            seconds = parse_duration(raw)
        except ValueError as exc:
            _fail(
                f"trigger {name!r}: 'lease' must be 'off' or a duration like '60m', "
                f"got {raw!r} ({exc})",
                file,
            )
        if seconds < 1:
            _fail(
                f"trigger {name!r}: 'lease' duration must be positive, got {raw!r}",
                file,
            )
        return seconds
    _fail(
        f"trigger {name!r}: 'lease' must be 'off' or a duration like '60m', got {raw!r}",
        file,
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
    for ``cairn schedule run <name>``.

    When the trigger declares ``lane:``, append ``--lane <name>`` so the host-watcher
    unit documents the autonomy profile (the drain also reads ``Trigger.lane`` from
    triggers.yaml when spawning each child ``cairn run``).
    """
    argv = [cairn_bin, "trigger", "run", trigger.name, "--workspace", str(workspace_dir)]
    if trigger.lane:
        argv += ["--lane", trigger.lane]
    return argv


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


def trigger_launchd_label(name: str, ws_id: str) -> str:
    """The launchd job label for a trigger (also the plist basename stem).

    Form: ``io.cairn.<ws8>.trigger.<name>`` — workspace-UUID-scoped so N factories on
    one machine never collide (FACTORY-PLAN §6 W3 / D9). The literal ``trigger.``
    segment — vs schedkit's :func:`launchd_label` ``io.cairn.<ws8>.<name>`` — means a
    trigger and a schedule sharing one name still never collide as LaunchAgent labels.

    Legacy (pre-W3) form was ``io.cairn.trigger.<name>``; :func:`sync_triggers` migrates
    this workspace's own old units by attributing ownership via ``--workspace`` in argv.
    """
    return f"io.cairn.{ws8(ws_id)}.trigger.{name}"


def trigger_launchd_label_legacy(name: str) -> str:
    """Pre-W3 launchd label (``io.cairn.trigger.<name>``) — migration discovery only."""
    return f"io.cairn.trigger.{name}"


def render_trigger_launchd(
    trigger: Trigger,
    workspace_dir: Path,
    cairn_bin: str,
    *,
    ws_id: str | None = None,
) -> str:
    """Render one trigger as a launchd LaunchAgent plist (XML string) with a ``WatchPaths``
    key, plistlib-generated (never hand-concatenated XML — malformed XML is a silently
    dead LaunchAgent, not a load-time error a human sees).

    ``ThrottleInterval: 10`` — WatchPaths fires on EVERY mutation inside the watched dir,
    including the ``.claim/``/``.done/``/``.failed/`` renames ``cairn trigger run`` itself
    performs while draining the inbox (TRIGGERS-PLAN.md §3). Without a floor, one real
    event can cascade into a burst of self-triggered re-fires; ``scan_candidates`` always
    excludes those dot-dirs, so each extra firing just scans an already-empty top level —
    a cheap no-op — which is what makes throttling to once per 10s safe rather than lossy.

    ``ws_id`` overrides the workspace UUID (render-only tests / offline paths that cannot
    mint ``.cairn/workspace-id``). Production callers leave it unset.
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
    wid = ws_id if ws_id is not None else workspace_id(workspace_dir)
    plist: dict[str, Any] = {
        "Label": trigger_launchd_label(trigger.name, wid),
        "ProgramArguments": _trigger_argv(trigger, workspace_dir, cairn_bin),
        "WatchPaths": [str(watch_abs)],
        "ThrottleInterval": 10,
    }
    return plistlib.dumps(plist, sort_keys=False).decode("utf-8")


def trigger_systemd_unit_names(name: str, ws_id: str) -> tuple[str, str]:
    """The (``.path``, ``.service``) unit filenames for a trigger.

    Form: ``cairn-<ws8>-trigger-<name>.{path,service}`` — workspace-scoped (W3 / D9).
    The ``trigger-`` segment — vs schedkit's ``cairn-<ws8>-<name>.{service,timer}`` —
    keeps a same-named schedule and trigger as distinct unit files. Legacy form was
    ``cairn-trigger-<name>.*``; sync migrates this workspace's own old units.
    """
    w = ws8(ws_id)
    return (f"cairn-{w}-trigger-{name}.path", f"cairn-{w}-trigger-{name}.service")


def trigger_systemd_unit_names_legacy(name: str) -> tuple[str, str]:
    """Pre-W3 systemd unit names — migration discovery only."""
    return (f"cairn-trigger-{name}.path", f"cairn-trigger-{name}.service")


def render_trigger_systemd(
    trigger: Trigger,
    workspace_dir: Path,
    cairn_bin: str,
    *,
    ws_id: str | None = None,
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

    ``ws_id`` overrides the workspace UUID (render-only tests). Production leaves it unset.
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
    wid = ws_id if ws_id is not None else workspace_id(workspace_dir)
    _, service_name = trigger_systemd_unit_names(trigger.name, wid)
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


def _workspace_from_argv(argv: list[str]) -> Path | None:
    """Extract ``--workspace <path>`` from a rendered host-unit argv, if present."""
    for i, tok in enumerate(argv):
        if tok == "--workspace" and i + 1 < len(argv):
            return Path(argv[i + 1])
        if tok.startswith("--workspace="):
            return Path(tok.split("=", 1)[1])
    return None


def _same_workspace(a: Path | None, b: Path) -> bool:
    """True when ``a`` names the same directory as ``b`` (resolved when possible)."""
    if a is None:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return Path(a) == Path(b)


def _classify_plist(path: Path) -> tuple[str, str | None, Path | None]:
    """Classify an existing plist from its OWN content — never trust the filename alone
    (spec finding F1 / addendum 2).

    Returns ``(kind, name, workspace)``:
    - ``("trigger", name, workspace_or_None)`` when argv is
      ``[cairn_bin, "trigger", "run", name, ..., --workspace <path>]``
    - ``("schedule", None, workspace_or_None)`` for schedkit's ``schedule run`` shape
      (or factory-reconcile argv)
    - ``("unmanaged", None, None)`` for foreign / unparseable content
    """
    try:
        doc = plistlib.loads(path.read_bytes())
    except Exception:
        return "unmanaged", None, None
    argv = doc.get("ProgramArguments")
    if not isinstance(argv, list) or len(argv) < 3 or not all(isinstance(a, str) for a in argv):
        return "unmanaged", None, None
    ws = _workspace_from_argv(argv)
    if len(argv) >= 4 and argv[1:3] == ["trigger", "run"]:
        return "trigger", argv[3], ws
    if len(argv) >= 3 and argv[1:2] == ["schedule"]:
        return "schedule", None, ws
    if len(argv) >= 3 and argv[1:3] == ["factory", "reconcile"]:
        return "schedule", None, ws
    return "unmanaged", None, None


def _classify_systemd_service(path: Path) -> tuple[str, str | None, Path | None]:
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
        # cairn-*-trigger-*.service in the target dir by _sync_systemd's prune sweep and
        # _installed_trigger_names — one stray binary file anywhere in that shared
        # directory must never crash the sweep for every trigger (review-T3-quality-r1.md
        # Finding 1).
        return "unmanaged", None, None
    exec_line = next((line for line in text.splitlines() if line.startswith("ExecStart=")), None)
    if exec_line is None:
        return "unmanaged", None, None
    try:
        argv = shlex.split(exec_line[len("ExecStart=") :])
    except ValueError:
        return "unmanaged", None, None
    ws = _workspace_from_argv(argv)
    if len(argv) >= 4 and argv[1:3] == ["trigger", "run"]:
        return "trigger", argv[3], ws
    if len(argv) >= 3 and argv[1:2] == ["schedule"]:
        return "schedule", None, ws
    if len(argv) >= 3 and argv[1:3] == ["factory", "reconcile"]:
        return "schedule", None, ws
    return "unmanaged", None, None


def _classify_systemd_path_unit(path: Path) -> tuple[str, str | None, Path | None]:
    """The ``.path``-file counterpart of :func:`_classify_plist`. Only triggerkit ever
    renders a ``.path`` unit — schedkit's systemd backend has no analogue — so this can
    never collide with a *schedule's* file the way ``.service`` can; an operator hand-
    placing an unrelated file at the managed stem is still possible, so content is
    checked all the same rather than trusting presence alone. Reads the ``Unit=`` line
    and recovers the trigger name from the ``cairn[-<ws8>]-trigger-<name>.service``
    target it names.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Same posture as _classify_systemd_service (see its comment) — non-UTF-8
        # content at this stem is foreign content, not a crash (review-T3-quality-r1.md
        # Finding 1).
        return "unmanaged", None, None
    unit_line = next((line for line in text.splitlines() if line.startswith("Unit=")), None)
    if unit_line is None:
        return "unmanaged", None, None
    target = unit_line[len("Unit=") :].strip()
    # New-style: cairn-<ws8>-trigger-<name>.service  | legacy: cairn-trigger-<name>.service
    match = re.fullmatch(r"cairn-(?:[0-9a-f]{8}-)?trigger-(.+)\.service", target)
    if not match:
        return "unmanaged", None, None
    return "trigger", match.group(1), None  # .path has no --workspace; ownership via .service


def _require_ownership(
    path: Path,
    trigger_name: str,
    classify: Callable[[Path], tuple[str, str | None, Path | None]],
    *,
    workspace_dir: Path | None = None,
) -> None:
    """Refuse to write or prune ``path`` unless it is either absent or recognizably OUR
    prior render for THIS trigger (spec finding F1, addendum 2 — a hard requirement,
    not a heuristic nicety): a schedule named ``trigger-X`` and a trigger named ``X``
    can still share a unit stem under the ws-scoped namespace, so ownership is verified
    from the existing file's content (via ``classify``) before it is ever touched.
    When ``workspace_dir`` is set, a trigger unit for a *different* workspace is also
    refused (multi-factory safety).
    """
    if not path.is_file():
        return
    owner_kind, owner_name, owner_ws = classify(path)
    if owner_kind == "trigger" and owner_name == trigger_name:
        if workspace_dir is None or owner_ws is None or _same_workspace(owner_ws, workspace_dir):
            return
    likely_owner = "a schedule" if owner_kind == "schedule" else "an unmanaged file"
    if owner_kind == "trigger" and owner_name == trigger_name and owner_ws is not None:
        likely_owner = f"another workspace's trigger ({owner_ws})"
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
    # W3/T13 lease surface (additive; 0/empty when leases off or no claims).
    lease_ttl_s: int | None = None  # effective ttl, or None when leases off
    lease_ages_s: tuple[float, ...] = ()
    expired_live: int = 0
    missing_lease: int = 0
    reaped: int = 0  # filled by reconcile summaries; list leaves 0


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

    Also: stamps ``ledger-version`` on every declared watch dir; installs (or removes)
    the workspace-scoped reconcile beat host timer when any (resp. no) trigger exists;
    migrates this workspace's own legacy unscoped units onto the ws-UUID label scheme
    without touching another workspace's units.

    Returns the unit/plist filenames installed, updated, or pruned this call (a trigger
    that was already current is still included — it's part of what's installed after
    this call returns, even though nothing was written for it).
    """
    workspace_dir = Path(workspace_dir)
    triggers = load_triggers(workspace_dir)
    # Stamp ledger-version on every declared watch dir (upgrade-safety marker).
    for trigger in triggers.values():
        stamp_ledger_version(watch_dir(trigger, workspace_dir))
    if backend == "cron":
        _cron_unsupported()
    if backend == "launchd":
        touched = _sync_launchd(triggers, workspace_dir, runner, cairn_bin, launchd_dir)
        _sync_reconcile_beat(
            bool(triggers),
            workspace_dir,
            backend="launchd",
            runner=runner,
            cairn_bin=cairn_bin,
            launchd_dir=launchd_dir,
            systemd_dir=None,
        )
        return touched
    if backend == "systemd":
        touched = _sync_systemd(triggers, workspace_dir, runner, cairn_bin, systemd_dir)
        _sync_reconcile_beat(
            bool(triggers),
            workspace_dir,
            backend="systemd",
            runner=runner,
            cairn_bin=cairn_bin,
            launchd_dir=None,
            systemd_dir=systemd_dir,
        )
        return touched
    _bad_backend(backend)


def _sync_launchd(
    triggers: dict[str, Trigger],
    workspace_dir: Path,
    runner: Runner,
    cairn_bin: str,
    launchd_dir: Path | None,
) -> list[str]:
    target = _require_dir(launchd_dir, "launchd", "launchd_dir")
    wid = workspace_id(workspace_dir)
    # Pre-pass: validate ownership for EVERY declared trigger before writing ANY of
    # them, so an ownership refusal for trigger N never leaves an earlier trigger's
    # freshly-written plist on disk from this same call (review-T3-quality-r1.md
    # Finding 3 — same "check everything, then act" pattern remove_trigger's systemd
    # branch now uses for its own two-stem check).
    for trigger in triggers.values():
        filename = f"{trigger_launchd_label(trigger.name, wid)}.plist"
        _require_ownership(
            target / filename, trigger.name, _classify_plist, workspace_dir=workspace_dir
        )
    touched: list[str] = []
    for trigger in triggers.values():
        filename = f"{trigger_launchd_label(trigger.name, wid)}.plist"
        path = target / filename
        rendered = render_trigger_launchd(trigger, workspace_dir, cairn_bin)
        if path.is_file() and path.read_text(encoding="utf-8") == rendered:
            touched.append(filename)  # already current — zero host-watcher calls
        else:
            path.write_text(rendered, encoding="utf-8")
            runner.run(["launchctl", "unload", str(path)])  # idempotent reload
            runner.run(["launchctl", "load", str(path)])
            touched.append(filename)
        # Migration: drop THIS workspace's legacy unscoped unit for the same name.
        legacy = target / f"{trigger_launchd_label_legacy(trigger.name)}.plist"
        if legacy.is_file() and legacy.resolve() != path.resolve():
            kind, name, ows = _classify_plist(legacy)
            if kind == "trigger" and name == trigger.name and _same_workspace(ows, workspace_dir):
                runner.run(["launchctl", "unload", str(legacy)])
                legacy.unlink()
                touched.append(legacy.name)
    # Prune: both new-style (this ws8) and legacy unscoped labels owned by THIS workspace
    # whose trigger left triggers.yaml. NEVER touch another workspace's units.
    for path in _iter_launchd_trigger_plists(target, wid):
        owner_kind, owner_name, owner_ws = _classify_plist(path)
        if owner_kind != "trigger" or owner_name is None:
            continue
        if not _same_workspace(owner_ws, workspace_dir):
            continue  # other factory's unit — leave it
        if owner_name in triggers and path.name == f"{trigger_launchd_label(owner_name, wid)}.plist":
            continue  # still declared, current-style — keep
        # Declared but we're looking at a legacy path already handled above, or
        # undeclared: prune.
        if owner_name in triggers:
            # legacy residual for a still-declared trigger — migrate pass should have
            # cleaned it; if still present (race), drop it.
            if path.name == f"{trigger_launchd_label_legacy(owner_name)}.plist":
                runner.run(["launchctl", "unload", str(path)])
                path.unlink()
                touched.append(path.name)
            continue
        runner.run(["launchctl", "unload", str(path)])
        path.unlink()
        touched.append(path.name)
    return touched


def _iter_launchd_trigger_plists(target: Path, wid: str):
    """Yield candidate launchd plists: new-style for this ws + legacy unscoped form."""
    seen: set[Path] = set()
    for pattern in (
        f"io.cairn.{ws8(wid)}.trigger.*.plist",
        "io.cairn.trigger.*.plist",
    ):
        for path in sorted(target.glob(pattern)):
            rp = path.resolve() if path.exists() else path
            if rp in seen:
                continue
            seen.add(rp)
            yield path


def _sync_systemd(
    triggers: dict[str, Trigger],
    workspace_dir: Path,
    runner: Runner,
    cairn_bin: str,
    systemd_dir: Path | None,
) -> list[str]:
    target = _require_dir(systemd_dir, "systemd", "systemd_dir")
    wid = workspace_id(workspace_dir)
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
        path_name, service_name = trigger_systemd_unit_names(trigger.name, wid)
        _require_ownership(
            target / path_name, trigger.name, _classify_systemd_path_unit, workspace_dir=workspace_dir
        )
        _require_ownership(
            target / service_name, trigger.name, _classify_systemd_service, workspace_dir=workspace_dir
        )

    touched: list[str] = []
    to_enable: list[str] = []
    needs_reload = False
    for trigger in triggers.values():
        path_name, service_name = trigger_systemd_unit_names(trigger.name, wid)
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
        if not unchanged:
            path_unit_path.write_text(path_text, encoding="utf-8")
            service_unit_path.write_text(service_text, encoding="utf-8")
            needs_reload = True
            to_enable.append(path_name)
        # Migration: drop THIS workspace's legacy unscoped units for the same name.
        leg_path, leg_service = trigger_systemd_unit_names_legacy(trigger.name)
        for leg_name, classify in (
            (leg_path, _classify_systemd_path_unit),
            (leg_service, _classify_systemd_service),
        ):
            leg = target / leg_name
            if not leg.is_file() or leg.name in (path_name, service_name):
                continue
            kind, name, ows = classify(leg)
            # .path units have no workspace in content — attribute via matching .service
            if leg.suffix == ".path":
                svc = target / leg_service
                if svc.is_file():
                    _, _, ows = _classify_systemd_service(svc)
            if kind == "trigger" and name == trigger.name and (
                ows is None or _same_workspace(ows, workspace_dir)
            ):
                # Only drop legacy when the service side confirms this workspace (or
                # when only a .path exists with no service to contradict).
                if leg.suffix == ".service" and not _same_workspace(ows, workspace_dir):
                    continue
                if leg.suffix == ".path":
                    runner.run(["systemctl", "--user", "disable", "--now", leg.name])
                leg.unlink()
                touched.append(leg.name)
                needs_reload = True
    # Prune managed files whose trigger left triggers.yaml (new-style + legacy),
    # but ONLY units owned by THIS workspace.
    for path in _iter_systemd_trigger_units(target, wid, "*.path"):
        owner_kind, owner_name, _ows = _classify_systemd_path_unit(path)
        if owner_kind != "trigger" or owner_name is None:
            continue
        # Attribute ownership via the paired .service when present.
        svc_guess = path.with_suffix(".service")
        if not svc_guess.is_file():
            # try legacy/new name pairing
            if path.name.startswith("cairn-trigger-"):
                svc_guess = target / path.name.replace(".path", ".service")
            else:
                svc_guess = path.with_suffix(".service")
        owner_ws = None
        if svc_guess.is_file():
            sk, sn, owner_ws = _classify_systemd_service(svc_guess)
            if sk == "trigger":
                owner_name = sn or owner_name
        if owner_ws is not None and not _same_workspace(owner_ws, workspace_dir):
            continue
        # If we have no workspace signal and the unit is legacy (shared namespace), only
        # prune when the path is new-style for this ws8 (unique to us) OR when the
        # paired service confirms ownership. Legacy without service: leave alone if
        # ambiguous — over-preserve.
        is_new_style = f"-{ws8(wid)}-trigger-" in path.name
        if owner_ws is None and not is_new_style:
            continue
        if owner_name in triggers and is_new_style:
            continue
        if owner_name in triggers and not is_new_style:
            # legacy residual for declared trigger
            runner.run(["systemctl", "--user", "disable", "--now", path.name])
            path.unlink()
            touched.append(path.name)
            needs_reload = True
            continue
        if owner_name not in triggers:
            runner.run(["systemctl", "--user", "disable", "--now", path.name])
            path.unlink()
            touched.append(path.name)
            needs_reload = True
    for path in _iter_systemd_trigger_units(target, wid, "*.service"):
        owner_kind, owner_name, owner_ws = _classify_systemd_service(path)
        if owner_kind != "trigger" or owner_name is None:
            continue
        if owner_ws is not None and not _same_workspace(owner_ws, workspace_dir):
            continue
        is_new_style = f"-{ws8(wid)}-trigger-" in path.name
        if owner_ws is None and not is_new_style:
            continue
        if owner_name in triggers and is_new_style:
            continue
        if owner_name in triggers or owner_name not in triggers:
            if owner_name in triggers and is_new_style:
                continue
            path.unlink()
            touched.append(path.name)
            needs_reload = True
    if needs_reload:
        runner.run(["systemctl", "--user", "daemon-reload"])
    for path_name in to_enable:
        runner.run(["systemctl", "--user", "enable", "--now", path_name])
    return touched


def _iter_systemd_trigger_units(target: Path, wid: str, suffix_glob: str):
    """Yield new-style + legacy trigger unit paths matching ``suffix_glob`` (e.g. ``*.path``)."""
    seen: set[Path] = set()
    patterns = (
        f"cairn-{ws8(wid)}-trigger-{suffix_glob}",
        f"cairn-trigger-{suffix_glob}",
    )
    for pattern in patterns:
        for path in sorted(target.glob(pattern)):
            rp = path.resolve() if path.exists() else path
            if rp in seen:
                continue
            seen.add(rp)
            yield path


def _beat_unit_paths(
    workspace_dir: Path,
    *,
    backend: str,
    launchd_dir: Path | None,
    systemd_dir: Path | None,
) -> list[Path]:
    """Paths of the reconcile-beat unit files for this workspace (may not exist yet)."""
    from cairn.kernel.schedkit import launchd_label, systemd_unit_names

    prefix = label_prefix_for(workspace_dir)
    wid = workspace_id(workspace_dir)
    if backend == "launchd":
        target = _require_dir(launchd_dir, "launchd", "launchd_dir")
        return [target / f"{launchd_label(RECONCILE_BEAT_NAME, prefix)}.plist"]
    if backend == "systemd":
        target = _require_dir(systemd_dir, "systemd", "systemd_dir")
        service, timer = systemd_unit_names(RECONCILE_BEAT_NAME, ws_id=wid)
        return [target / service, target / timer]
    return []


def _beat_owned_by_workspace(path: Path, workspace_dir: Path, backend: str) -> bool | None:
    """Whether an existing beat unit belongs to ``workspace_dir``.

    Returns:
    - ``True`` — content/argv attributes this unit to this workspace (safe to
      overwrite or remove)
    - ``False`` — unit exists and belongs to a *different* workspace (never touch)
    - ``None`` — absent, unparseable, or no ``--workspace`` signal (treat as
      unmanaged: install may claim; uninstall must not delete)
    """
    if not path.is_file():
        return None
    if backend == "launchd":
        kind, _name, ows = _classify_plist(path)
    else:
        # Prefer .service (carries ExecStart/--workspace); .timer has no argv.
        if path.suffix == ".timer":
            svc = path.with_suffix(".service")
            if svc.is_file():
                kind, _name, ows = _classify_systemd_service(svc)
            else:
                return None
        else:
            kind, _name, ows = _classify_systemd_service(path)
    if ows is None:
        return None
    return _same_workspace(ows, workspace_dir)


def _require_beat_writable(path: Path, workspace_dir: Path, backend: str) -> None:
    """Refuse to write a beat unit owned by another workspace (C1 / multi-factory)."""
    owned = _beat_owned_by_workspace(path, workspace_dir, backend)
    if owned is False:
        message = (
            f"refusing to write reconcile beat unit {path}: it already exists and "
            f"belongs to another workspace (content --workspace argv does not match "
            f"{workspace_dir}). Multi-factory safety: never overwrite another "
            "factory's reconcile beat (FACTORY-PLAN §6 W3 / T14 C1)."
        )
        raise ConfigError(message, findings=[Finding("error", message)], file=str(path))


def _sync_reconcile_beat(
    want: bool,
    workspace_dir: Path,
    *,
    backend: str,
    runner: Runner,
    cairn_bin: str,
    launchd_dir: Path | None,
    systemd_dir: Path | None,
) -> None:
    """Install or remove the ws-scoped reconcile host timer via schedkit.

    When any trigger exists (``want=True``), install a calendar timer firing
    ``cairn factory reconcile --workspace <ws>`` every 10m with RunAtLoad/boot.
    When the last trigger is removed, drop the beat. Idempotent.

    **Ownership (T14 C1):** every write/delete is content-verified against this
    workspace's ``--workspace`` argv — same discipline as trigger units. A beat
    unit belonging to another factory is never removed or overwritten.
    """
    from cairn.kernel.schedkit import (
        Schedule,
        install as sched_install,
        uninstall_named,
    )

    workspace_dir = Path(workspace_dir)
    prefix = label_prefix_for(workspace_dir)
    wid = workspace_id(workspace_dir)
    argv = [cairn_bin, "factory", "reconcile", "--workspace", str(workspace_dir)]
    unit_paths = _beat_unit_paths(
        workspace_dir,
        backend=backend,
        launchd_dir=launchd_dir,
        systemd_dir=systemd_dir,
    )

    if want:
        # Pre-pass: refuse if any existing unit is owned by another workspace.
        for path in unit_paths:
            _require_beat_writable(path, workspace_dir, backend)
        schedule = Schedule(
            name=RECONCILE_BEAT_NAME,
            cron=RECONCILE_BEAT_CRON,
            run=("factory", "reconcile"),
        )
        sched_install(
            {RECONCILE_BEAT_NAME: schedule},
            backend,
            workspace_dir=workspace_dir,
            runner=runner,
            cairn_bin=cairn_bin,
            launchd_dir=launchd_dir,
            systemd_dir=systemd_dir,
            label_prefix=prefix,
            program_arguments=argv,
            run_at_load=True,
            ws_id=wid,
        )
        return

    # Uninstall: only touch units this workspace owns. A foreign beat at the same
    # stem (ws8 collision / stale) is left alone — never filename-only delete.
    for path in unit_paths:
        owned = _beat_owned_by_workspace(path, workspace_dir, backend)
        if owned is not True:
            # Absent, foreign, or unattributed — do not delete.
            continue
    # Only call uninstall_named when at least one unit is ours.
    ours = [
        p
        for p in unit_paths
        if _beat_owned_by_workspace(p, workspace_dir, backend) is True
    ]
    if not ours:
        return
    uninstall_named(
        [RECONCILE_BEAT_NAME],
        backend,
        runner=runner,
        launchd_dir=launchd_dir,
        systemd_dir=systemd_dir,
        label_prefix=prefix,
        ws_id=wid,
    )


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
    share the stem. Also drops a legacy unscoped unit for this workspace if present.
    When the last trigger unit is gone, removes the reconcile beat.
    """
    workspace_dir = Path(workspace_dir)
    wid = workspace_id(workspace_dir)
    if backend == "cron":
        _cron_unsupported()
    if backend == "launchd":
        target = _require_dir(launchd_dir, backend, "launchd_dir")
        removed = False
        for label in (
            trigger_launchd_label(name, wid),
            trigger_launchd_label_legacy(name),
        ):
            path = target / f"{label}.plist"
            if not path.is_file():
                continue
            kind, oname, ows = _classify_plist(path)
            if kind != "trigger" or oname != name:
                _require_ownership(path, name, _classify_plist, workspace_dir=workspace_dir)
                continue
            if ows is not None and not _same_workspace(ows, workspace_dir):
                continue  # other workspace's unit at legacy stem — leave it
            _require_ownership(path, name, _classify_plist, workspace_dir=workspace_dir)
            runner.run(["launchctl", "unload", str(path)])
            path.unlink()
            removed = True
        if removed:
            _maybe_drop_reconcile_beat(
                workspace_dir,
                backend="launchd",
                runner=runner,
                cairn_bin="cairn",
                launchd_dir=launchd_dir,
                systemd_dir=None,
            )
        return removed
    if backend == "systemd":
        target = _require_dir(systemd_dir, backend, "systemd_dir")
        pairs = [
            trigger_systemd_unit_names(name, wid),
            trigger_systemd_unit_names_legacy(name),
        ]
        # Validate ownership of every existing stem before acting on any.
        for path_name, service_name in pairs:
            path_unit_path = target / path_name
            service_unit_path = target / service_name
            if path_unit_path.is_file():
                _require_ownership(
                    path_unit_path, name, _classify_systemd_path_unit, workspace_dir=workspace_dir
                )
            if service_unit_path.is_file():
                kind, oname, ows = _classify_systemd_service(service_unit_path)
                if kind == "trigger" and oname == name and ows is not None and not _same_workspace(
                    ows, workspace_dir
                ):
                    continue  # other ws
                _require_ownership(
                    service_unit_path, name, _classify_systemd_service, workspace_dir=workspace_dir
                )
        removed = False
        for path_name, service_name in pairs:
            path_unit_path = target / path_name
            service_unit_path = target / service_name
            if service_unit_path.is_file():
                kind, oname, ows = _classify_systemd_service(service_unit_path)
                if kind == "trigger" and oname == name and ows is not None and not _same_workspace(
                    ows, workspace_dir
                ):
                    continue
            if path_unit_path.is_file():
                runner.run(["systemctl", "--user", "disable", "--now", path_name])
                path_unit_path.unlink()
                removed = True
            if service_unit_path.is_file():
                kind, oname, ows = _classify_systemd_service(service_unit_path)
                if kind == "trigger" and oname == name and (
                    ows is None or _same_workspace(ows, workspace_dir)
                ):
                    service_unit_path.unlink()
                    removed = True
        if removed:
            runner.run(["systemctl", "--user", "daemon-reload"])
            _maybe_drop_reconcile_beat(
                workspace_dir,
                backend="systemd",
                runner=runner,
                cairn_bin="cairn",
                launchd_dir=None,
                systemd_dir=systemd_dir,
            )
        return removed
    _bad_backend(backend)


def _maybe_drop_reconcile_beat(
    workspace_dir: Path,
    *,
    backend: str,
    runner: Runner,
    cairn_bin: str,
    launchd_dir: Path | None,
    systemd_dir: Path | None,
) -> None:
    """Remove the reconcile beat when no declared triggers remain (post-remove)."""
    if load_triggers(workspace_dir):
        return
    # Also check host for any remaining installed units for this ws.
    try:
        installed = _installed_trigger_names(
            backend,
            workspace_dir=workspace_dir,
            launchd_dir=launchd_dir,
            systemd_dir=systemd_dir,
        )
    except ConfigError:
        installed = set()
    if installed:
        return
    _sync_reconcile_beat(
        False,
        workspace_dir,
        backend=backend,
        runner=runner,
        cairn_bin=cairn_bin,
        launchd_dir=launchd_dir,
        systemd_dir=systemd_dir,
    )


def _installed_trigger_names(
    backend: str,
    *,
    workspace_dir: Path | None = None,
    launchd_dir: Path | None = None,
    systemd_dir: Path | None = None,
) -> set[str]:
    """The set of trigger names with a recognizably trigger-owned unit/plist file on the
    host for THIS workspace (when ``workspace_dir`` is set), read back straight from the
    target dir — no `runner` needed for launchd/systemd.
    """
    if backend == "launchd":
        target = _require_dir(launchd_dir, backend, "launchd_dir")
        wid = workspace_id(workspace_dir) if workspace_dir is not None else None
        names: set[str] = set()
        patterns = ["io.cairn.trigger.*.plist"]
        if wid is not None:
            patterns.insert(0, f"io.cairn.{ws8(wid)}.trigger.*.plist")
        else:
            patterns.insert(0, "io.cairn.*.trigger.*.plist")
        seen: set[Path] = set()
        for pattern in patterns:
            for path in target.glob(pattern):
                rp = path.resolve() if path.exists() else path
                if rp in seen:
                    continue
                seen.add(rp)
                owner_kind, owner_name, owner_ws = _classify_plist(path)
                if owner_kind != "trigger" or owner_name is None:
                    continue
                if workspace_dir is not None and owner_ws is not None:
                    if not _same_workspace(owner_ws, workspace_dir):
                        continue
                names.add(owner_name)
        return names
    if backend == "systemd":
        target = _require_dir(systemd_dir, backend, "systemd_dir")
        wid = workspace_id(workspace_dir) if workspace_dir is not None else None
        names = set()
        patterns = ["cairn-trigger-*.service"]
        if wid is not None:
            patterns.insert(0, f"cairn-{ws8(wid)}-trigger-*.service")
        else:
            patterns.insert(0, "cairn-*-trigger-*.service")
        seen = set()
        for pattern in patterns:
            for path in target.glob(pattern):
                rp = path.resolve() if path.exists() else path
                if rp in seen:
                    continue
                seen.add(rp)
                owner_kind, owner_name, owner_ws = _classify_systemd_service(path)
                if owner_kind != "trigger" or owner_name is None:
                    continue
                if workspace_dir is not None and owner_ws is not None:
                    if not _same_workspace(owner_ws, workspace_dir):
                        continue
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
    installed_names = _installed_trigger_names(
        backend,
        workspace_dir=workspace_dir,
        launchd_dir=launchd_dir,
        systemd_dir=systemd_dir,
    )
    statuses: list[TriggerStatus] = []
    for name in sorted(set(triggers) | installed_names):
        trigger = triggers.get(name)
        stuck: tuple[Path, ...] = ()
        waiting = failed = done = 0
        needs_human = blocked = capacity = inflight = spool = 0
        concurrency = 1
        order = "name"
        waiting_max = blocked_max = capacity_max = wip_max = inbox_max = None
        lease_ttl: int | None = None
        lease_ages: tuple[float, ...] = ()
        expired_live = missing_lease = 0
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
            lease_ttl = effective_lease_ttl(trigger.lease_ttl_s, trigger.concurrency)
            if lease_ttl is not None:
                ls = lease_status(watch_abs)
                lease_ages = tuple(ls["lease_ages_s"])
                expired_live = int(ls["expired_live"])
                missing_lease = int(ls["missing_lease"])
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
                lease_ttl_s=lease_ttl,
                lease_ages_s=lease_ages,
                expired_live=expired_live,
                missing_lease=missing_lease,
            )
        )
    return statuses
