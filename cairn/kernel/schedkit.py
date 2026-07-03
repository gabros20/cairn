"""schedkit — first-class scheduling without a scheduler (docs/SCHEDULING.md).

cairn does not own a clock or run a daemon; the host scheduler (cron / launchd /
systemd timers) fires ``cairn schedule run <name>`` and cairn makes itself perfectly
schedulable. This module is the engine behind the ``cairn schedule`` verb:

- :func:`load_schedules` — parse + validate ``schedules.yaml`` into typed
  :class:`Schedule` objects, with precise :class:`ConfigError`\\ s (unknown keys, bad
  cron expressions, unknown pipelines fail loudly at parse time where checkable).
- :func:`idempotency_key` / :func:`find_idempotent_run` — the pure predicate that makes
  a scheduled ``cairn run --idempotent`` a no-op (or a resume) when an equivalent
  successful run already exists. This is the heart of "scheduling without a scheduler".
- :func:`render_cron` / :func:`render_launchd` / :func:`render_systemd` — RENDER-ONLY
  functions returning the exact host-scheduler text, fully unit-testable offline.
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
from typing import Any, NoReturn, Protocol

import yaml

from cairn.kernel.errors import ConfigError
from cairn.kernel.types import Finding

SCHEDULES_YAML = "schedules.yaml"

# The leading verbs a schedule's `run:` argv may invoke (SCHEDULING.md §1: "schedules can
# invoke run, batch, resume, gc, or the self-improve pipeline" — self-improve is `run self-improve`).
_ALLOWED_VERBS = frozenset({"run", "batch", "resume", "gc"})
# Verbs whose FIRST positional token is a pipeline name we can check against pipelines/.
_PIPELINE_VERBS = frozenset({"run", "batch"})
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
) -> str:
    """Render one schedule as a launchd LaunchAgent plist (XML string).

    A single-interval schedule renders StartCalendarInterval as one dict; a schedule that
    expands to several fire-times renders it as an array (launchd's native repeat mechanism).
    """
    intervals = _calendar_intervals(parse_cron(schedule.cron))
    plist: dict[str, Any] = {
        "Label": launchd_label(schedule.name, label_prefix),
        "ProgramArguments": [cairn_bin, "schedule", "run", schedule.name],
        "WorkingDirectory": str(workspace_dir),
        "StartCalendarInterval": intervals[0] if len(intervals) == 1 else intervals,
        "RunAtLoad": False,
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


def systemd_unit_names(name: str) -> tuple[str, str]:
    """The (service, timer) unit filenames for a schedule."""
    return (f"cairn-{name}.service", f"cairn-{name}.timer")


def render_systemd(
    schedule: Schedule, *, workspace_dir: Path, cairn_bin: str = "cairn"
) -> tuple[str, str]:
    """Render one schedule as a systemd (service, timer) unit pair (text strings).

    The timer is ``Persistent=true`` so a missed firing (machine asleep) runs at next boot —
    the same catch-up-via-resume posture ``--idempotent`` gives (SCHEDULING.md §3).
    """
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
        f"OnCalendar={_on_calendar(parse_cron(schedule.cron))}\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


# --------------------------------------------------------------------------- #
# Effectful verbs — every side effect is dependency-injected (SCHEDULING.md §2)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RunResult:
    """The outcome of one injected host-command invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    """The injected effect boundary — the only thing that actually touches the host.

    Tests pass a fake; the CLI (Wave 2) passes a subprocess-backed adapter. Keeping this the
    sole side-effect surface is what lets every render/plan path stay pure and offline.
    """

    def run(self, argv: list[str], *, input: str | None = None, cwd: Path | None = None) -> RunResult:
        ...


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
) -> None:
    """Sync ``schedules`` into the host scheduler via the injected ``runner``. Idempotent."""
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
            path.write_text(
                render_launchd(schedule, workspace_dir=workspace_dir, cairn_bin=cairn_bin,
                               label_prefix=label_prefix),
                encoding="utf-8",
            )
            runner.run(["launchctl", "unload", str(path)])  # idempotent reload
            runner.run(["launchctl", "load", str(path)])
    elif backend == "systemd":
        target = _require_dir(systemd_dir, backend, "systemd_dir")
        for schedule in schedules.values():
            service_name, timer_name = systemd_unit_names(schedule.name)
            service, timer = render_systemd(schedule, workspace_dir=workspace_dir, cairn_bin=cairn_bin)
            (target / service_name).write_text(service, encoding="utf-8")
            (target / timer_name).write_text(timer, encoding="utf-8")
        runner.run(["systemctl", "--user", "daemon-reload"])
        for schedule in schedules.values():
            _, timer_name = systemd_unit_names(schedule.name)
            runner.run(["systemctl", "--user", "enable", "--now", timer_name])
    else:
        _bad_backend(backend)


def uninstall(
    backend: str,
    *,
    workspace_dir: Path,
    runner: Runner,
    launchd_dir: Path | None = None,
    systemd_dir: Path | None = None,
    label_prefix: str = "io.cairn.",
) -> None:
    """Remove every cairn-managed entry from the host scheduler. Idempotent; foreign entries stay."""
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
        for timer in sorted(target.glob("cairn-*.timer")):
            runner.run(["systemctl", "--user", "disable", "--now", timer.name])
            timer.unlink()
        for service in sorted(target.glob("cairn-*.service")):
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
                fields = line.split()
                cron_expr = " ".join(fields[:5])
                name = fields[-1]  # ... schedule run <name>
                entries[name] = InstalledEntry(name=name, schedule=cron_expr)
    elif backend == "launchd":
        target = _require_dir(launchd_dir, backend, "launchd_dir")
        for path in sorted(target.glob(f"{label_prefix}*.plist")):
            name = path.stem[len(label_prefix):]
            entries[name] = InstalledEntry(name=name, schedule=None)
    elif backend == "systemd":
        target = _require_dir(systemd_dir, backend, "systemd_dir")
        for path in sorted(target.glob("cairn-*.timer")):
            name = path.stem[len("cairn-"):]
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
) -> int:
    """Execute one schedule's action NOW, returning the child cairn's exit code.

    Composes the argv as ``[cairn_bin, *schedule.run]`` and runs it in the workspace via the
    injected ``runner`` — this is also exactly what the host timer calls (SCHEDULING.md §2).
    The argv is verbatim from schedules.yaml: ``--idempotent`` / ``--headless`` are opt-in there,
    never injected here (§3). Pinned shape: ``[cairn_bin] + list(schedule.run)``.
    """
    if name not in schedules:
        raise ConfigError(
            f"no schedule named {name!r} in schedules.yaml",
            findings=[Finding("error", f"unknown schedule {name!r}")],
        )
    argv = [cairn_bin, *schedules[name].run]
    return runner.run(argv, cwd=Path(workspace_dir)).returncode


def _bad_backend(backend: str) -> NoReturn:
    raise ConfigError(
        f"unknown backend {backend!r} (expected cron, launchd, or systemd)",
        findings=[Finding("error", f"unknown backend {backend!r}")],
    )
