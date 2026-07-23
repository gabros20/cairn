"""schedkit — first-class scheduling without a scheduler (docs/SCHEDULING.md).

cairn does not own a clock or run a daemon; the host scheduler (cron / launchd /
systemd timers) fires ``cairn schedule run <name>`` and cairn makes itself perfectly
schedulable. This module is the engine behind the ``cairn schedule`` verb:

- :func:`load_schedules` — parse + validate ``schedules.yaml`` into typed
  :class:`Schedule` objects, with precise :class:`ConfigError`\\ s (unknown keys, bad
  cron expressions, unknown pipelines fail loudly at parse time where checkable). A
  scheduled ``run``/``batch``/``resume`` MUST carry ``--headless`` — a schedule can never
  block on a human (SCHEDULING.md §4); ``gc`` and ``trigger`` are exempt (inherently
  non-interactive — a fired trigger's own child run is already ``--headless`` by
  construction, TRIGGERS.md §3).
- :func:`idempotency_key` / :func:`find_idempotent_run` — the pure predicate that makes
  a scheduled ``cairn run --idempotent`` a no-op (or a resume) when an equivalent
  successful run already exists. This is the heart of "scheduling without a scheduler".
  Identity is bucketed by calendar day (mirroring the ``{date}`` a run_id embeds), so a
  firing that straddles midnight — or a systemd ``Persistent=true`` catch-up that lands the
  next day — is treated as a NEW run, not a duplicate of the prior day's.
- :func:`render_cron` / :func:`render_launchd` / :func:`render_systemd` — RENDER-ONLY
  functions returning the exact host-scheduler text, fully unit-testable offline. cron gets
  the expression verbatim; launchd/systemd REJECT a schedule that restricts both day-of-month
  and day-of-week (cron's OR semantics, which their AND-only calendars cannot express).
- :func:`install` / :func:`uninstall` / :func:`list_installed` / :func:`run_schedule` —
  the effectful verbs. Every side effect (crontab / launchctl / systemctl / filesystem)
  is dependency-injected via a :class:`Runner` and explicit target dirs, so tests never
  touch the real host scheduler or ``~/Library``.

stdlib + pyyaml only. Clock is always injected (``now``); no hidden ``datetime.now()``.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import plistlib
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn, TextIO

import yaml

from cairn.kernel.errors import ConfigError
from cairn.kernel.proc import RunResult as RunResult  # re-export for back-compat (cli.py, tests)
from cairn.kernel.proc import Runner as Runner  # re-export for back-compat
from cairn.kernel.types import Finding

SCHEDULES_YAML = "schedules.yaml"

# The leading verbs a schedule's `run:` argv may invoke (SCHEDULING.md §1: "schedules can
# invoke run, batch, resume, gc, or the self-improve pipeline" — self-improve is `run self-improve`).
# "trigger" is here so the cron-refusal fallback TRIGGERS.md §3 documents (poll a trigger's
# inbox via a schedules.yaml entry invoking `trigger run <name>`) is actually loadable — it
# gets the same non-interactive treatment as `gc` (below): never forced into _HEADLESS_VERBS,
# because a fired trigger's own child run is already --headless by construction
# (triggerkit.run_trigger → `cairn run ... --headless`), so there is nothing for THIS argv to
# enforce.
_ALLOWED_VERBS = frozenset({"run", "batch", "resume", "gc", "trigger"})
# Verbs whose FIRST positional token is a pipeline name we can check against pipelines/.
_PIPELINE_VERBS = frozenset({"run", "batch"})
# Verbs that spawn an agent run and so MUST be headless when scheduled (SCHEDULING.md §4:
# "scheduled runs are headless runs ... simply required here"). gc is inherently non-interactive.
_HEADLESS_VERBS = frozenset({"run", "batch", "resume"})
_SCHEDULE_KEYS = frozenset({"cron", "run"})


# --------------------------------------------------------------------------- #
# Typed model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Schedule:
    """One declared schedule: a name, a host cron expression, and the cairn argv to fire."""

    name: str
    cron: str
    run: tuple[str, ...]


def _fail(message: str, file: Path) -> NoReturn:
    raise ConfigError(message, findings=[Finding("error", message)], file=str(file))


# --------------------------------------------------------------------------- #
# load_schedules
# --------------------------------------------------------------------------- #


def load_schedules(workspace_dir: Path) -> dict[str, Schedule]:
    """Load ``<workspace_dir>/schedules.yaml`` into name → :class:`Schedule`.

    Raises :class:`ConfigError` (naming the offending schedule/field) on a missing file,
    malformed YAML, unknown keys, a bad cron expression, a disallowed verb, or a ``run``/
    ``batch`` entry that names a pipeline with no ``pipelines/<name>.yaml``.
    """
    workspace_dir = Path(workspace_dir)
    file = workspace_dir / SCHEDULES_YAML
    if not file.is_file():
        _fail(f"no schedules.yaml found in {workspace_dir}", file)

    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        _fail(f"schedules.yaml is not valid YAML: {exc}", file)

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _fail("schedules.yaml must be a mapping of name → schedule", file)

    schedules: dict[str, Schedule] = {}
    for name, entry in raw.items():
        schedules[str(name)] = _parse_schedule(str(name), entry, workspace_dir, file)
    return schedules


def _parse_schedule(name: str, entry: Any, workspace_dir: Path, file: Path) -> Schedule:
    if not isinstance(entry, dict):
        _fail(f"schedule {name!r} must be a mapping with 'cron' and 'run'", file)
    for key in entry:
        if key not in _SCHEDULE_KEYS:
            _fail(f"schedule {name!r}: unknown key {key!r} (allowed: cron, run)", file)

    cron = entry.get("cron")
    if not isinstance(cron, str) or not cron.strip():
        _fail(f"schedule {name!r} requires a non-empty string 'cron'", file)
    try:
        parse_cron(cron)
    except ValueError as exc:
        _fail(f"schedule {name!r}: invalid cron {cron!r}: {exc}", file)

    run = entry.get("run")
    if not isinstance(run, list) or not run:
        _fail(f"schedule {name!r} requires a non-empty list 'run' (a cairn argv)", file)
    if not all(isinstance(tok, str) for tok in run):
        _fail(f"schedule {name!r}: every element of 'run' must be a string", file)

    verb = run[0]
    if verb not in _ALLOWED_VERBS:
        _fail(
            f"schedule {name!r}: run verb {verb!r} not allowed "
            f"(allowed: {', '.join(sorted(_ALLOWED_VERBS))})",
            file,
        )
    if verb in _HEADLESS_VERBS and "--headless" not in run:
        _fail(
            f"schedule {name!r}: a scheduled {verb!r} must be headless — add --headless "
            "to its 'run' argv (SCHEDULING.md §4: a schedule can never block on a human)",
            file,
        )
    if verb in _PIPELINE_VERBS:
        if len(run) < 2 or run[1].startswith("-"):
            _fail(f"schedule {name!r}: '{verb}' requires a pipeline name", file)
        pipeline = run[1]
        pfile = workspace_dir / "pipelines" / f"{pipeline}.yaml"
        if not pfile.is_file():
            _fail(
                f"schedule {name!r}: unknown pipeline {pipeline!r} "
                f"(no {pfile})",
                file,
            )

    return Schedule(name=name, cron=cron.strip(), run=tuple(run))


# --------------------------------------------------------------------------- #
# Cron parsing (the reusable engine behind every backend renderer)
# --------------------------------------------------------------------------- #

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DOWS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")

# @-macros that have a plain calendar equivalent (crontab's @reboot has none → rejected).
_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


@dataclass(frozen=True)
class CronField:
    """One parsed cron field: either a wildcard, or the explicit sorted values it matches.

    ``wildcard`` fields render as ``*`` everywhere; explicit fields are expanded to their
    full integer set so every backend renderer shares one uniform representation (comma
    lists for cron/systemd, a cartesian product for launchd's StartCalendarInterval).
    """

    wildcard: bool
    values: tuple[int, ...]  # () when wildcard


@dataclass(frozen=True)
class CronSpec:
    """A normalized 5-field cron expression (minute, hour, dom, month, dow)."""

    minute: CronField
    hour: CronField
    dom: CronField
    month: CronField
    dow: CronField


def _parse_field(text: str, lo: int, hi: int, names: tuple[str, ...] = ()) -> CronField:
    """Parse one cron field over ``[lo, hi]`` into a :class:`CronField`.

    Supports ``*``, ``*/step``, ``a``, ``a-b``, ``a-b/step``, ``a/step`` and comma lists
    of those, plus 3-letter month/day names when ``names`` is given. Raises ValueError with
    a precise message on anything else or any out-of-range value.
    """
    name_map = {n: lo + i for i, n in enumerate(names)}

    def _num(tok: str) -> int:
        key = tok.lower()
        if key in name_map:
            return name_map[key]
        if not re.fullmatch(r"\d+", tok):
            raise ValueError(f"{tok!r} is not a number or a known name")
        return int(tok)

    values: set[int] = set()
    for item in text.split(","):
        item = item.strip()
        if not item:
            raise ValueError("empty item in list")
        base, _, step_s = item.partition("/")
        step = 1
        if _:
            if not re.fullmatch(r"\d+", step_s) or int(step_s) < 1:
                raise ValueError(f"invalid step {step_s!r} in {item!r}")
            step = int(step_s)

        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a_s, _, b_s = base.partition("-")
            start, end = _num(a_s), _num(b_s)
            if start > end:
                raise ValueError(f"range {base!r} is descending")
        else:
            start = _num(base)
            end = hi if _ else start  # `a/step` means a..max step; bare `a` is a single value

        for v in range(start, end + 1, step):
            if not lo <= v <= hi:
                raise ValueError(f"value {v} out of range {lo}-{hi}")
            values.add(v)

    if not values:
        raise ValueError("field matched no values")
    # A `*`/`*/1` whole-range field is a wildcard; a `*/step` that skips is explicit.
    is_wildcard = text.strip() == "*"
    return CronField(wildcard=is_wildcard, values=() if is_wildcard else tuple(sorted(values)))


def parse_cron(expr: str) -> CronSpec:
    """Parse a 5-field cron expression (or an ``@macro``) into a normalized :class:`CronSpec`.

    Day-of-week accepts 0-7 (0 and 7 are both Sunday, normalized to 0). Raises ValueError
    with a precise message on a wrong field count, a bad token, or an out-of-range value.
    ``@reboot`` is rejected — it has no calendar time and does not fit the host-evaluates-time
    model (SCHEDULING.md §5).
    """
    text = expr.strip()
    if text.startswith("@"):
        if text == "@reboot":
            raise ValueError("@reboot is not a calendar schedule and is unsupported")
        if text not in _MACROS:
            raise ValueError(f"unknown macro {text!r}")
        text = _MACROS[text]

    parts = text.split()
    if len(parts) != 5:
        raise ValueError(f"expected 5 fields, got {len(parts)}")

    minute = _parse_field(parts[0], 0, 59)
    hour = _parse_field(parts[1], 0, 23)
    dom = _parse_field(parts[2], 1, 31)
    month = _parse_field(parts[3], 1, 12, _MONTHS)
    dow_raw = _parse_field(parts[4], 0, 7, _DOWS)
    # Normalize 7 → 0 (both Sunday) and de-dup.
    if dow_raw.wildcard:
        dow = dow_raw
    else:
        dow = CronField(
            wildcard=False,
            values=tuple(sorted({0 if v == 7 else v for v in dow_raw.values})),
        )

    return CronSpec(minute=minute, hour=hour, dom=dom, month=month, dow=dow)


# --------------------------------------------------------------------------- #
# Idempotency — the primitive that makes timers safe (SCHEDULING.md §3)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IdempotentMatch:
    """An existing run equivalent to the one a scheduled `--idempotent` firing would create.

    ``complete`` distinguishes the two safe outcomes (§3): a *completed* equivalent run means
    the firing is a **no-op** (exit 0); an incomplete one means **resume** it rather than
    creating a variant.
    """

    run_dir: Path
    run_id: str
    complete: bool


def idempotency_key(*, pipeline: str, params: Mapping[str, Any], now: datetime) -> str:
    """A stable content key identifying a scheduled invocation on a given day.

    The key is a sha256 over the canonical ``(pipeline, params, {date})`` — the same
    identity the resolved ``run_id`` embeds (§3: "it already embeds ``{date}``"). Two firings
    the same day with equal params share a key (the re-fire is a no-op / resume); the next
    day's firing gets a new key (a new run). ``dims`` are omitted deliberately — they are
    derived from ``params`` and add nothing to identity. ``now`` is the injected clock; the
    date bucket is ``now`` formatted ``%Y%m%d``.
    """
    payload = {
        "pipeline": pipeline,
        "params": {str(k): params[k] for k in sorted(params)},
        "date": now.strftime("%Y%m%d"),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _run_key(doc: Mapping[str, Any]) -> str | None:
    """The idempotency key of an on-disk run, bucketed by its own ``created_at`` date."""
    try:
        created = datetime.fromisoformat(str(doc["created_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    params = doc.get("params") or {}
    if not isinstance(params, Mapping):
        return None
    return idempotency_key(pipeline=str(doc.get("pipeline", "")), params=params, now=created)


def find_idempotent_run(
    runs_root: Path, *, pipeline: str, params: Mapping[str, Any], now: datetime
) -> IdempotentMatch | None:
    """Scan ``runs_root`` for a run equivalent to a ``--idempotent`` firing of this argv.

    Returns the matching :class:`IdempotentMatch` (``complete`` tells the caller skip-vs-resume),
    or ``None`` when no equivalent run exists and a fresh run should be created. A completed
    match wins over an incomplete one if both somehow exist. Unreadable/invalid run dirs are
    skipped, never fatal — this is a best-effort read of prior state.
    """
    runs_root = Path(runs_root)
    if not runs_root.is_dir():
        return None
    want = idempotency_key(pipeline=pipeline, params=params, now=now)

    resume: IdempotentMatch | None = None
    for child in sorted(runs_root.iterdir()):
        run_json = child / "run.json"
        if not run_json.is_file():
            continue
        try:
            doc = json.loads(run_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict) or _run_key(doc) != want:
            continue
        match = IdempotentMatch(
            run_dir=child,
            run_id=str(doc.get("run_id", child.name)),
            complete=doc.get("status") == "done",
        )
        if match.complete:
            return match  # a completed equivalent run — the firing is a no-op
        resume = resume or match
    return resume


# --------------------------------------------------------------------------- #
# Backend rendering — RENDER-ONLY, fully offline (SCHEDULING.md §2)
# --------------------------------------------------------------------------- #

_CRON_BEGIN = "# >>> cairn schedules (managed) — edit schedules.yaml, not here >>>"
_CRON_END = "# <<< cairn schedules (managed) <<<"
_CRON_REGION = re.compile(re.escape(_CRON_BEGIN) + r".*?" + re.escape(_CRON_END), re.DOTALL)

_LAUNCHD_KEYS = (("Minute", "minute"), ("Hour", "hour"), ("Day", "dom"),
                 ("Month", "month"), ("Weekday", "dow"))
_SYSTEMD_DOW = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def _host_command(schedule: Schedule, workspace_dir: Path, cairn_bin: str) -> str:
    """The one line the host scheduler runs: cd into the workspace, then `schedule run <name>`.

    Always ``schedule run <name>`` — never the expanded argv — so editing schedules.yaml
    changes behavior without re-touching the host scheduler (SCHEDULING.md §2).
    """
    return (
        f"cd {shlex.quote(str(workspace_dir))} && "
        f"{shlex.quote(cairn_bin)} schedule run {shlex.quote(schedule.name)}"
    )


def _parse_managed_cron_line(line: str) -> tuple[str, str] | None:
    """Recover ``(name, cron_expr)`` from one managed crontab line, or None if it isn't ours.

    Rather than assume a fixed 5-field cron (which garbles ``@daily`` and other macros), split on
    the known command anchor ``" cd "`` — a cron expression never contains it — so the cron expr
    is everything before the command, whatever its token count. The schedule name is recovered by
    shlex-splitting the command tail, so names with spaces survive the round-trip intact.
    """
    cron_expr, sep, command = line.partition(" cd ")
    if not sep:
        return None
    try:
        tokens = shlex.split("cd " + command)
    except ValueError:
        return None
    if len(tokens) < 3 or tokens[-3:-1] != ["schedule", "run"]:
        return None
    return tokens[-1], cron_expr.strip()


def render_cron(
    schedules: Mapping[str, Schedule], *, workspace_dir: Path, cairn_bin: str = "cairn"
) -> str:
    """Render a marker-fenced crontab block — one ``<cron> <command>`` line per schedule."""
    lines = [_CRON_BEGIN]
    for schedule in schedules.values():
        lines.append(f"{schedule.cron} {_host_command(schedule, Path(workspace_dir), cairn_bin)}")
    lines.append(_CRON_END)
    return "\n".join(lines)


def merge_cron(existing: str, block: str) -> str:
    """Splice ``block`` into ``existing``, replacing any prior managed region in place.

    Foreign crontab entries outside the markers are never touched (SCHEDULING.md §2).
    """
    if _CRON_REGION.search(existing):
        return _CRON_REGION.sub(lambda _m: block, existing)
    base = existing if existing == "" or existing.endswith("\n") else existing + "\n"
    return base + block + "\n"


def strip_cron(existing: str) -> str:
    """Remove the managed region, leaving foreign crontab entries intact."""
    result = _CRON_REGION.sub("", existing)
    return result.rstrip("\n") + "\n" if result.strip() else ""


def launchd_label(name: str, prefix: str = "io.cairn.") -> str:
    """The launchd job label for a schedule (also the plist basename stem)."""
    return f"{prefix}{name}"


def _reject_dom_and_dow(spec: CronSpec, name: str, backend: str) -> None:
    """Refuse a schedule that restricts BOTH day-of-month and day-of-week.

    In cron such a schedule means "the 1st OR a Monday" (a union). launchd's interval array
    and systemd's OnCalendar both AND their components, so a faithful translation is impossible
    — rather than silently install a schedule that fires on the wrong days, fail loud.
    """
    if not spec.dom.wildcard and not spec.dow.wildcard:
        raise ConfigError(
            f"schedule {name!r}: cron restricts both day-of-month and day-of-week "
            f"(an OR that the {backend} backend cannot express, only AND). Use the cron "
            "backend, or split it into two schedules (one per day rule).",
            findings=[Finding("error", f"{name}: dom+dow OR not expressible in {backend}")],
        )


def _calendar_intervals(spec: CronSpec) -> list[dict[str, int]]:
    """Expand a CronSpec into launchd StartCalendarInterval dicts (cartesian over set fields)."""
    axes: list[tuple[str, tuple[int, ...]]] = []
    for launchd_key, attr in _LAUNCHD_KEYS:
        field: CronField = getattr(spec, attr)
        if not field.wildcard:
            axes.append((launchd_key, field.values))
    if not axes:
        return [{}]  # all wildcard → every minute (an empty StartCalendarInterval)
    keys = [k for k, _ in axes]
    return [dict(zip(keys, combo)) for combo in itertools.product(*(v for _, v in axes))]


def render_launchd(
    schedule: Schedule,
    *,
    workspace_dir: Path,
    cairn_bin: str = "cairn",
    label_prefix: str = "io.cairn.",
    program_arguments: list[str] | None = None,
    run_at_load: bool = False,
) -> str:
    """Render one schedule as a launchd LaunchAgent plist (XML string).

    A single-interval schedule renders StartCalendarInterval as one dict; a schedule that
    expands to several fire-times renders it as an array (launchd's native repeat mechanism).

    ``program_arguments`` overrides the default ``[cairn_bin, schedule, run, name]`` argv
    (used by the factory reconcile beat to fire ``factory reconcile --workspace`` directly).
    ``run_at_load`` sets RunAtLoad (boot/load fire) — default False for ordinary schedules.
    """
    spec = parse_cron(schedule.cron)
    _reject_dom_and_dow(spec, schedule.name, "launchd")
    intervals = _calendar_intervals(spec)
    argv = (
        list(program_arguments)
        if program_arguments is not None
        else [cairn_bin, "schedule", "run", schedule.name]
    )
    plist: dict[str, Any] = {
        "Label": launchd_label(schedule.name, label_prefix),
        "ProgramArguments": argv,
        "WorkingDirectory": str(workspace_dir),
        "StartCalendarInterval": intervals[0] if len(intervals) == 1 else intervals,
        "RunAtLoad": bool(run_at_load),
    }
    return plistlib.dumps(plist, sort_keys=False).decode("utf-8")


def _field_csv(field: CronField, pad: int = 0) -> str:
    """A systemd calendar component: ``*`` for a wildcard, else a comma list (optionally padded)."""
    if field.wildcard:
        return "*"
    return ",".join(str(v).zfill(pad) for v in field.values)


def _on_calendar(spec: CronSpec) -> str:
    """Translate a CronSpec into a systemd ``OnCalendar=`` expression."""
    date = f"*-{_field_csv(spec.month)}-{_field_csv(spec.dom)}"
    time = f"{_field_csv(spec.hour, 2)}:{_field_csv(spec.minute, 2)}:00"
    if spec.dow.wildcard:
        return f"{date} {time}"
    dow = ",".join(_SYSTEMD_DOW[v] for v in spec.dow.values)
    return f"{dow} {date} {time}"


def systemd_unit_names(name: str, ws_id: str | None = None) -> tuple[str, str]:
    """The (service, timer) unit filenames for a schedule.

    When ``ws_id`` is set (multi-factory / W3), names are ``cairn-<ws8>-<name>.*`` so
    N workspaces on one machine never collide. When absent, the legacy unscoped
    ``cairn-<name>.*`` form is kept (pure schedkit unit tests / single-ws default).
    """
    if ws_id:
        w = ws_id.replace("-", "").lower()[:8]
        return (f"cairn-{w}-{name}.service", f"cairn-{w}-{name}.timer")
    return (f"cairn-{name}.service", f"cairn-{name}.timer")


def render_systemd(
    schedule: Schedule,
    *,
    workspace_dir: Path,
    cairn_bin: str = "cairn",
    program_arguments: list[str] | None = None,
    ws_id: str | None = None,
) -> tuple[str, str]:
    """Render one schedule as a systemd (service, timer) unit pair (text strings).

    The timer is ``Persistent=true`` so a missed firing (machine asleep) runs at next boot —
    the same catch-up-via-resume posture ``--idempotent`` gives (SCHEDULING.md §3).

    ``program_arguments`` overrides the default ``cairn schedule run <name>`` ExecStart
    (factory reconcile beat uses a direct ``factory reconcile --workspace`` argv).
    """
    spec = parse_cron(schedule.cron)
    _reject_dom_and_dow(spec, schedule.name, "systemd")
    if program_arguments is not None:
        command = shlex.join(list(program_arguments))
    else:
        command = f"{cairn_bin} schedule run {schedule.name}"
    service = (
        "[Unit]\n"
        f"Description=cairn schedule: {schedule.name}\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={workspace_dir}\n"
        f"ExecStart={command}\n"
    )
    timer = (
        "[Unit]\n"
        f"Description=cairn schedule timer: {schedule.name}\n\n"
        "[Timer]\n"
        f"OnCalendar={_on_calendar(spec)}\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


# --------------------------------------------------------------------------- #
# Effectful verbs — every side effect is dependency-injected (SCHEDULING.md §2)
# --------------------------------------------------------------------------- #


# RunResult / Runner now live in cairn.kernel.proc (the shared subprocess seam, unified with
# batchkit's spawn) and are re-exported at the top of this module for back-compat. See proc.py
# for why schedkit injects a Runner OBJECT (needs input=/optional cwd) while batchkit injects a
# bare (argv, cwd) -> tuple callable over the same SubprocessRunner.


@dataclass(frozen=True)
class InstalledEntry:
    """One schedule as read back from the host. ``schedule`` is the host expr when recoverable
    (the cron expression for the cron backend, systemd's ``OnCalendar`` for systemd), else None
    (launchd's StartCalendarInterval is not reversed to a cron string)."""

    name: str
    schedule: str | None


@dataclass(frozen=True)
class ScheduleDiff:
    """declared-vs-installed, as the ``schedule list`` verb reports it (SCHEDULING.md §2)."""

    added: tuple[str, ...]      # declared, not installed
    removed: tuple[str, ...]    # installed, not declared
    changed: tuple[str, ...]    # both present, host schedule differs (where comparable)
    unchanged: tuple[str, ...]  # both present, identical (or not comparably different)


def _read_crontab(runner: Runner) -> str:
    """Current crontab text, or "" when the user has no crontab (``crontab -l`` exits non-zero)."""
    res = runner.run(["crontab", "-l"])
    return res.stdout if res.returncode == 0 else ""


def _require_dir(value: Path | None, backend: str, param: str) -> Path:
    if value is None:
        raise ConfigError(
            f"{backend} backend requires an explicit {param} (the target directory)",
            findings=[Finding("error", f"{backend}: missing {param}")],
        )
    path = Path(value)
    path.mkdir(parents=True, exist_ok=True)
    return path


def install(
    schedules: Mapping[str, Schedule],
    backend: str,
    *,
    workspace_dir: Path,
    runner: Runner,
    cairn_bin: str = "cairn",
    launchd_dir: Path | None = None,
    systemd_dir: Path | None = None,
    label_prefix: str = "io.cairn.",
    program_arguments: list[str] | None = None,
    run_at_load: bool = False,
    ws_id: str | None = None,
) -> None:
    """Sync ``schedules`` into the host scheduler via the injected ``runner``. Idempotent.

    ``program_arguments`` / ``run_at_load`` apply to every schedule in this call (used by
    the single-entry factory reconcile beat). ``ws_id`` scopes systemd unit names.
    """
    workspace_dir = Path(workspace_dir)
    if backend == "cron":
        merged = merge_cron(
            _read_crontab(runner), render_cron(schedules, workspace_dir=workspace_dir, cairn_bin=cairn_bin)
        )
        runner.run(["crontab", "-"], input=merged)
    elif backend == "launchd":
        target = _require_dir(launchd_dir, backend, "launchd_dir")
        for schedule in schedules.values():
            path = target / f"{launchd_label(schedule.name, label_prefix)}.plist"
            rendered = render_launchd(
                schedule,
                workspace_dir=workspace_dir,
                cairn_bin=cairn_bin,
                label_prefix=label_prefix,
                program_arguments=program_arguments,
                run_at_load=run_at_load,
            )
            # True no-op when already current (same posture as trigger_host sync).
            if path.is_file() and path.read_text(encoding="utf-8") == rendered:
                continue
            path.write_text(rendered, encoding="utf-8")
            runner.run(["launchctl", "unload", str(path)])  # idempotent reload
            runner.run(["launchctl", "load", str(path)])
    elif backend == "systemd":
        target = _require_dir(systemd_dir, backend, "systemd_dir")
        needs_reload = False
        to_enable: list[str] = []
        for schedule in schedules.values():
            service_name, timer_name = systemd_unit_names(schedule.name, ws_id=ws_id)
            service, timer = render_systemd(
                schedule,
                workspace_dir=workspace_dir,
                cairn_bin=cairn_bin,
                program_arguments=program_arguments,
                ws_id=ws_id,
            )
            sp, tp = target / service_name, target / timer_name
            unchanged = (
                sp.is_file()
                and sp.read_text(encoding="utf-8") == service
                and tp.is_file()
                and tp.read_text(encoding="utf-8") == timer
            )
            if unchanged:
                continue
            sp.write_text(service, encoding="utf-8")
            tp.write_text(timer, encoding="utf-8")
            needs_reload = True
            to_enable.append(timer_name)
        if needs_reload:
            runner.run(["systemctl", "--user", "daemon-reload"])
            for timer_name in to_enable:
                runner.run(["systemctl", "--user", "enable", "--now", timer_name])
    else:
        _bad_backend(backend)


def uninstall_named(
    names: list[str],
    backend: str,
    *,
    runner: Runner,
    launchd_dir: Path | None = None,
    systemd_dir: Path | None = None,
    label_prefix: str = "io.cairn.",
    ws_id: str | None = None,
) -> None:
    """Remove specific named schedule units only (not the whole managed set). Idempotent."""
    if backend == "cron":
        # Cron is a single managed block — selective name removal is not supported here.
        return
    if backend == "launchd":
        target = _require_dir(launchd_dir, backend, "launchd_dir")
        for name in names:
            path = target / f"{launchd_label(name, label_prefix)}.plist"
            if path.is_file():
                runner.run(["launchctl", "unload", str(path)])
                path.unlink()
        return
    if backend == "systemd":
        target = _require_dir(systemd_dir, backend, "systemd_dir")
        removed = False
        for name in names:
            service_name, timer_name = systemd_unit_names(name, ws_id=ws_id)
            timer_path = target / timer_name
            service_path = target / service_name
            if timer_path.is_file():
                runner.run(["systemctl", "--user", "disable", "--now", timer_name])
                timer_path.unlink()
                removed = True
            if service_path.is_file():
                service_path.unlink()
                removed = True
        if removed:
            runner.run(["systemctl", "--user", "daemon-reload"])
        return
    _bad_backend(backend)


def uninstall(
    backend: str,
    *,
    workspace_dir: Path,
    runner: Runner,
    launchd_dir: Path | None = None,
    systemd_dir: Path | None = None,
    label_prefix: str = "io.cairn.",
    ws_id: str | None = None,
) -> None:
    """Remove every cairn-managed entry from the host scheduler. Idempotent; foreign entries stay.

    When ``ws_id`` is set, systemd removal is scoped to ``cairn-<ws8>-*`` only so
    another factory's units on the same machine are never touched.
    """
    if backend == "cron":
        stripped = strip_cron(_read_crontab(runner))
        runner.run(["crontab", "-"], input=stripped)
    elif backend == "launchd":
        target = _require_dir(launchd_dir, backend, "launchd_dir")
        for path in sorted(target.glob(f"{label_prefix}*.plist")):
            runner.run(["launchctl", "unload", str(path)])
            path.unlink()
    elif backend == "systemd":
        target = _require_dir(systemd_dir, backend, "systemd_dir")
        if ws_id:
            w = ws_id.replace("-", "").lower()[:8]
            timer_glob, service_glob = f"cairn-{w}-*.timer", f"cairn-{w}-*.service"
        else:
            timer_glob, service_glob = "cairn-*.timer", "cairn-*.service"
        for timer in sorted(target.glob(timer_glob)):
            runner.run(["systemctl", "--user", "disable", "--now", timer.name])
            timer.unlink()
        for service in sorted(target.glob(service_glob)):
            service.unlink()
        runner.run(["systemctl", "--user", "daemon-reload"])
    else:
        _bad_backend(backend)


def list_installed(
    backend: str,
    *,
    runner: Runner | None = None,
    launchd_dir: Path | None = None,
    systemd_dir: Path | None = None,
    label_prefix: str = "io.cairn.",
    ws_id: str | None = None,
) -> dict[str, InstalledEntry]:
    """Read back what cairn has installed on the host (the injected reader), keyed by name."""
    entries: dict[str, InstalledEntry] = {}
    if backend == "cron":
        if runner is None:
            raise ConfigError("cron backend requires a runner to read the crontab")
        region = _CRON_REGION.search(_read_crontab(runner))
        if region:
            for line in region.group(0).splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                parsed = _parse_managed_cron_line(line)
                if parsed is not None:
                    name, cron_expr = parsed
                    entries[name] = InstalledEntry(name=name, schedule=cron_expr)
    elif backend == "launchd":
        target = _require_dir(launchd_dir, backend, "launchd_dir")
        for path in sorted(target.glob(f"{label_prefix}*.plist")):
            name = path.stem[len(label_prefix):]
            entries[name] = InstalledEntry(name=name, schedule=None)
    elif backend == "systemd":
        target = _require_dir(systemd_dir, backend, "systemd_dir")
        if ws_id:
            w = ws_id.replace("-", "").lower()[:8]
            prefix = f"cairn-{w}-"
            glob_pat = f"cairn-{w}-*.timer"
        else:
            prefix = "cairn-"
            glob_pat = "cairn-*.timer"
        for path in sorted(target.glob(glob_pat)):
            name = path.stem[len(prefix):]
            on_calendar = None
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("OnCalendar="):
                    on_calendar = line[len("OnCalendar="):]
            entries[name] = InstalledEntry(name=name, schedule=on_calendar)
    else:
        _bad_backend(backend)
    return entries


def diff_schedules(
    declared: Mapping[str, Schedule], installed: Mapping[str, InstalledEntry]
) -> ScheduleDiff:
    """Diff declared schedules against what's installed (SCHEDULING.md §2 `schedule list`).

    ``changed`` fires only when both sides carry a comparable host schedule string and it
    differs (true for the cron backend, where the installed line's cron expr is exact).
    """
    added = tuple(n for n in declared if n not in installed)
    removed = tuple(n for n in installed if n not in declared)
    changed: list[str] = []
    unchanged: list[str] = []
    for name in declared:
        if name not in installed:
            continue
        want = declared[name].cron
        have = installed[name].schedule
        if have is not None and have != want:
            changed.append(name)
        else:
            unchanged.append(name)
    return ScheduleDiff(
        added=added, removed=removed, changed=tuple(changed), unchanged=tuple(unchanged)
    )


def run_schedule(
    schedules: Mapping[str, Schedule],
    name: str,
    *,
    workspace_dir: Path,
    runner: Runner,
    cairn_bin: str = "cairn",
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Execute one schedule's action NOW, returning the child cairn's exit code.

    Composes the argv as ``[cairn_bin, *schedule.run]`` and runs it in the workspace via the
    injected ``runner`` — this is also exactly what the host timer calls (SCHEDULING.md §2).
    The argv is verbatim from schedules.yaml: ``--idempotent`` / ``--headless`` are opt-in there,
    never injected here (§3). Pinned shape: ``[cairn_bin] + list(schedule.run)``.

    The Runner captures the child's stdout/stderr, so a firing that halts (e.g. NEEDS_HUMAN=6
    with a resume hint) would otherwise produce ZERO output and the host mailer would send
    nothing — silently rotting, which §4 forbids. When ``out``/``err`` are provided, the
    captured streams are re-emitted VERBATIM to them after the child completes, so cron mails
    the halt reason and the resume hint. When they are None, nothing is re-emitted (the prior
    silent behavior, kept for backward compatibility). Return-code semantics are unchanged.
    """
    if name not in schedules:
        raise ConfigError(
            f"no schedule named {name!r} in schedules.yaml",
            findings=[Finding("error", f"unknown schedule {name!r}")],
        )
    argv = [cairn_bin, *schedules[name].run]
    result = runner.run(argv, cwd=Path(workspace_dir))
    if out is not None and result.stdout:
        out.write(result.stdout)
    if err is not None and result.stderr:
        err.write(result.stderr)
    return result.returncode


def _bad_backend(backend: str) -> NoReturn:
    raise ConfigError(
        f"unknown backend {backend!r} (expected cron, launchd, or systemd)",
        findings=[Finding("error", f"unknown backend {backend!r}")],
    )
