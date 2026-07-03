"""Load and validate ``cairn.toml`` into typed, frozen config dataclasses.

Parsing is stdlib-only (``tomllib``). Unknown top-level keys surface as warning
Findings on the returned Config; malformed values raise ConfigError with a precise
message naming the offending key.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from cairn.kernel.errors import ConfigError
from cairn.kernel.types import EFFORTS, TIERS, Finding

_DURATION_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")
_UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}

# Top-level tables cairn understands. Anything else is surfaced as a warning, not an error,
# so an unrecognised block never silently breaks a load.
_KNOWN_TOP_LEVEL = {
    "workspace",
    "defaults",
    "tools",
    "executors",
    "secrets",
    "sinks",
    "requires",
}


def parse_duration(text: str) -> int:
    """Parse a duration string like ``"30m"``, ``"45s"``, ``"2h"``, ``"1h30m"`` into seconds.

    A bare integer is read as seconds (``"90"`` → 90). Anything else — empty, unknown
    units, decimals, signs, embedded spaces — raises ValueError.
    """
    if not isinstance(text, str):
        raise ValueError(f"duration must be a string, got {type(text).__name__}")
    if text.isdigit():
        return int(text)
    m = _DURATION_RE.fullmatch(text)
    if m is None or not any(m.groups()):
        raise ValueError(
            f"invalid duration {text!r}: expected e.g. '30m', '45s', '2h', '1h30m'"
        )
    hours, minutes, seconds = m.groups()
    return (
        int(hours or 0) * _UNIT_SECONDS["h"]
        + int(minutes or 0) * _UNIT_SECONDS["m"]
        + int(seconds or 0) * _UNIT_SECONDS["s"]
    )


# --------------------------------------------------------------------------- #
# The workspace `requires` version pin (docs/DISTRIBUTION.md §3).
#
# A workspace may pin the cairn it needs — `requires = ">=0.1,<0.2"` at the top
# level of cairn.toml — and the planner refuses when the installed cairn falls
# outside the range. Stdlib-only PEP 440 subset: comma-separated clauses with
# the operators ==, !=, >=, <=, >, <, ~= plus the `==X.Y.*` prefix wildcard;
# versions are `N(.N)*` with optional (a|b|rc)N pre-release, `.postN`, `.devN`
# (a dev/pre-release orders BEFORE its release, per PEP 440). A bare clause
# with no operator means exact equality (releases compare zero-padded, so
# "0.1" == "0.1.0").
# --------------------------------------------------------------------------- #

_PRE_RANK = {"a": 0, "b": 1, "rc": 2}
_VERSION_RE = re.compile(
    r"v?(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?(?:\.post(\d+))?(?:\.dev(\d+))?"
)
_CLAUSE_OPS = ("~=", "==", "!=", ">=", "<=", ">", "<")

_Version = tuple[tuple[int, ...], tuple[int, int] | None, int | None, int | None]


def _parse_version(text: str) -> _Version:
    """``"0.1.0.dev0"`` → ``(release, pre, post, dev)``; ValueError on anything else."""
    m = _VERSION_RE.fullmatch(text.strip())
    if m is None:
        raise ValueError(f"invalid version {text!r}")
    release = tuple(int(part) for part in m.group(1).split("."))
    pre = (_PRE_RANK[m.group(2)], int(m.group(3))) if m.group(2) else None
    post = int(m.group(4)) if m.group(4) is not None else None
    dev = int(m.group(5)) if m.group(5) is not None else None
    return release, pre, post, dev


def _version_key(v: _Version, width: int) -> tuple:
    """A totally-ordered sort key; ``width`` zero-pads the release for comparison."""
    release, pre, post, dev = v
    rel = release + (0,) * (width - len(release))
    if pre is None and post is None and dev is not None:
        pre_key = (-1, 0, 0)  # a pure .devN sorts before every pre-release
    elif pre is None:
        pre_key = (1, 0, 0)  # a final release sorts after its pre-releases
    else:
        pre_key = (0, *pre)
    post_key = (0, 0) if post is None else (1, post)
    dev_key = (1, 0) if dev is None else (0, dev)
    return (rel, pre_key, post_key, dev_key)


def _clause_ok(op: str, installed: _Version, wanted: _Version, wildcard: bool) -> bool:
    if wildcard:  # ==X.Y.* / !=X.Y.* — prefix match on the release tuple
        prefix = wanted[0]
        padded = installed[0] + (0,) * max(0, len(prefix) - len(installed[0]))
        matches = padded[: len(prefix)] == prefix
        return matches if op == "==" else not matches
    if op == "~=":  # ~=X.Y.Z ⇒ >=X.Y.Z and ==X.Y.*
        if len(wanted[0]) < 2:
            raise ValueError("~= needs at least two release components (e.g. ~=0.1)")
        return _clause_ok(">=", installed, wanted, False) and _clause_ok(
            "==", installed, (wanted[0][:-1], None, None, None), True
        )
    width = max(len(installed[0]), len(wanted[0]))
    a, b = _version_key(installed, width), _version_key(wanted, width)
    return {
        "==": a == b,
        "!=": a != b,
        ">=": a >= b,
        "<=": a <= b,
        ">": a > b,
        "<": a < b,
    }[op]


def requires_satisfied(spec: str, installed: str) -> bool:
    """Does ``installed`` satisfy every clause of ``spec`` (e.g. ``">=0.1,<0.2"``)?

    Raises ValueError with the offending clause on a malformed spec.
    """
    inst = _parse_version(installed)
    clauses = [c.strip() for c in spec.split(",")]
    if not any(clauses):
        raise ValueError("empty requires spec")
    for clause in clauses:
        if not clause:
            raise ValueError(f"empty clause in requires spec {spec!r}")
        op = next((o for o in _CLAUSE_OPS if clause.startswith(o)), None)
        version_text = clause[len(op) :].strip() if op else clause
        op = op or "=="
        wildcard = version_text.endswith(".*")
        if wildcard:
            if op not in ("==", "!="):
                raise ValueError(f"'.*' is only valid with == or != (clause {clause!r})")
            version_text = version_text[:-2]
        if not _clause_ok(op, inst, _parse_version(version_text), wildcard):
            return False
    return True


def installed_version() -> str:
    """The installed cairn version — single source of truth is ``cairn.__version__``
    (pyproject reads the same string via hatch's dynamic version)."""
    import cairn

    return cairn.__version__


def version_compat(recorded: str | None, installed: str) -> str:
    """Classify a run's recorded cairn version against the ``installed`` one for the
    cross-version resume gate (docs/DISTRIBUTION.md §3, *Run-dir format*):

    - ``"ok"``     — same release, or same major.minor (patch-only drift): resume silently.
    - ``"warn"``   — cross-minor drift, an *unrecorded* (legacy) version, or an unparseable
      one: resume with a note, never refuse.
    - ``"refuse"`` — cross-major drift: run-dir semantics may not carry across a major, so
      resume only under ``--force``.

    A missing recorded version (``None``/empty) never refuses — it warns and proceeds.
    """
    if not recorded:
        return "warn"
    try:
        rec = _parse_version(recorded)[0]
        inst = _parse_version(installed)[0]
    except ValueError:
        return "warn"  # a version we can't parse must not hard-block a resume
    rec_major, inst_major = (rec[0] if rec else 0), (inst[0] if inst else 0)
    rec_minor = rec[1] if len(rec) > 1 else 0
    inst_minor = inst[1] if len(inst) > 1 else 0
    if rec_major != inst_major:
        return "refuse"
    if rec_minor != inst_minor:
        return "warn"
    return "ok"


def check_requires(
    requires: str | None, *, file: str | Path, installed: str | None = None
) -> None:
    """Enforce the workspace ``requires`` pin; no-op when the workspace doesn't pin.

    Raises ConfigError naming both the required range and the installed version when
    the pin is unsatisfied (or the spec itself is malformed).
    """
    if requires is None:
        return
    version = installed if installed is not None else installed_version()
    try:
        ok = requires_satisfied(requires, version)
    except ValueError as exc:
        message = f"cairn.toml requires: {exc}"
        raise ConfigError(
            message, findings=[Finding("error", message)], file=str(file)
        ) from exc
    if not ok:
        message = (
            f"this workspace requires cairn {requires!r} but cairn {version} is "
            f"installed — install a matching version (e.g. `uv tool install "
            f"'cairn{requires}'`) or update the requires pin in cairn.toml"
        )
        raise ConfigError(message, findings=[Finding("error", message)], file=str(file))


# --------------------------------------------------------------------------- #
# Typed config dataclasses (all frozen — config is immutable during a run).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Workspace:
    name: str
    doctrine: str | None = None
    runs_dir: str = "runs"
    default_executor: str | None = None


@dataclass(frozen=True)
class TrailContext:
    events: int = 12
    learnings: int = 5


@dataclass(frozen=True)
class Budget:
    run_usd: float | None = None
    step_usd: float | None = None


@dataclass(frozen=True)
class Defaults:
    step_timeout_s: int = 1800
    trail_context: TrailContext = field(default_factory=TrailContext)
    heartbeat_s: int | None = None
    budget: Budget | None = None


@dataclass(frozen=True)
class TierSpec:
    model: str
    effort: str | None = None


@dataclass(frozen=True)
class ExecutorConfig:
    name: str
    enabled: bool = True
    pin_version: str | None = None
    setup: str | None = None
    tiers: dict[str, TierSpec] = field(default_factory=dict)
    flags: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolConfig:
    name: str
    check: str
    install: str | None = None
    needed_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SecretConfig:
    name: str
    needed_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Config:
    workspace: Workspace
    defaults: Defaults
    executors: dict[str, ExecutorConfig]
    tools: dict[str, ToolConfig]
    secrets: dict[str, SecretConfig]
    sinks: dict[str, dict[str, Any]]
    requires: str | None
    warnings: list[Finding]


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #


def _fail(message: str, file: Path) -> NoReturn:
    raise ConfigError(message, findings=[Finding("error", message)], file=str(file))


def _require_table(value: Any, where: str, file: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"[{where}] must be a table in cairn.toml", file)
    return value


def _require_int(value: Any, where: str, file: Path) -> int:
    # bool is a subclass of int — reject it so `events = true` doesn't silently become 1.
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{where} must be an integer, got {value!r}", file)
    return value


def _require_number(value: Any, where: str, file: Path) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{where} must be a number, got {value!r}", file)
    return value


def _require_bool(value: Any, where: str, file: Path) -> bool:
    if not isinstance(value, bool):
        _fail(f"{where} must be true or false, got {value!r}", file)
    return value


def _warn_unknown(
    table: dict[str, Any], known: set[str], where: str, warnings: list[Finding]
) -> None:
    for key in table:
        if key not in known:
            warnings.append(
                Finding("warning", f"unknown key {key!r} in [{where}] — ignored")
            )


def _parse_duration_field(raw: dict[str, Any], key: str, where: str, file: Path) -> int:
    value = raw[key]
    if not isinstance(value, str):
        _fail(f"{where}.{key} must be a duration string like '30m', got {value!r}", file)
    try:
        return parse_duration(value)
    except ValueError as exc:
        _fail(f"{where}.{key}: {exc}", file)


def _parse_workspace(raw: Any, file: Path, warnings: list[Finding]) -> Workspace:
    table = _require_table(raw, "workspace", file)
    _warn_unknown(
        table, {"name", "doctrine", "runs_dir", "default_executor"}, "workspace", warnings
    )
    name = table.get("name")
    if not isinstance(name, str) or not name:
        _fail("[workspace] requires a non-empty string 'name'", file)
    return Workspace(
        name=name,
        doctrine=table.get("doctrine"),
        runs_dir=table.get("runs_dir", "runs"),
        default_executor=table.get("default_executor"),
    )


def _parse_defaults(raw: Any, file: Path, warnings: list[Finding]) -> Defaults:
    table = _require_table(raw, "defaults", file)
    _warn_unknown(
        table, {"step_timeout", "trail_context", "heartbeat", "budget"}, "defaults", warnings
    )

    step_timeout_s = (
        _parse_duration_field(table, "step_timeout", "defaults", file)
        if "step_timeout" in table
        else 1800
    )
    heartbeat_s = (
        _parse_duration_field(table, "heartbeat", "defaults", file)
        if "heartbeat" in table
        else None
    )

    tc_raw = _require_table(table.get("trail_context", {}), "defaults.trail_context", file)
    _warn_unknown(tc_raw, {"events", "learnings"}, "defaults.trail_context", warnings)
    trail_context = TrailContext(
        events=_require_int(tc_raw["events"], "defaults.trail_context.events", file)
        if "events" in tc_raw
        else 12,
        learnings=_require_int(tc_raw["learnings"], "defaults.trail_context.learnings", file)
        if "learnings" in tc_raw
        else 5,
    )

    budget = None
    if "budget" in table:
        b_raw = _require_table(table["budget"], "defaults.budget", file)
        _warn_unknown(b_raw, {"run_usd", "step_usd"}, "defaults.budget", warnings)
        budget = Budget(
            run_usd=_require_number(b_raw["run_usd"], "defaults.budget.run_usd", file)
            if "run_usd" in b_raw
            else None,
            step_usd=_require_number(b_raw["step_usd"], "defaults.budget.step_usd", file)
            if "step_usd" in b_raw
            else None,
        )

    return Defaults(
        step_timeout_s=step_timeout_s,
        trail_context=trail_context,
        heartbeat_s=heartbeat_s,
        budget=budget,
    )


def _parse_tier(
    name: str, exec_name: str, raw: Any, file: Path, warnings: list[Finding]
) -> TierSpec:
    if name not in TIERS:
        _fail(
            f"executors.{exec_name}.tiers: unknown tier {name!r} "
            f"(valid tiers: {', '.join(TIERS)})",
            file,
        )
    table = _require_table(raw, f"executors.{exec_name}.tiers.{name}", file)
    _warn_unknown(table, {"model", "effort"}, f"executors.{exec_name}.tiers.{name}", warnings)
    model = table.get("model")
    if not isinstance(model, str) or not model:
        _fail(f"executors.{exec_name}.tiers.{name} requires a string 'model'", file)
    effort = table.get("effort")
    if effort is not None and effort not in EFFORTS:
        _fail(
            f"executors.{exec_name}.tiers.{name}.effort {effort!r} invalid "
            f"(valid: {', '.join(EFFORTS)})",
            file,
        )
    return TierSpec(model=model, effort=effort)


def _parse_executor(
    name: str, raw: Any, file: Path, warnings: list[Finding]
) -> ExecutorConfig:
    table = _require_table(raw, f"executors.{name}", file)
    # `flags` is an open table (arbitrary argv the executor appends) — not key-checked.
    _warn_unknown(
        table, {"enabled", "pin_version", "setup", "tiers", "flags"}, f"executors.{name}", warnings
    )
    enabled = (
        _require_bool(table["enabled"], f"executors.{name}.enabled", file)
        if "enabled" in table
        else True
    )
    tiers_raw = _require_table(table.get("tiers", {}), f"executors.{name}.tiers", file)
    tiers = {t: _parse_tier(t, name, spec, file, warnings) for t, spec in tiers_raw.items()}
    flags = _require_table(table.get("flags", {}), f"executors.{name}.flags", file)
    return ExecutorConfig(
        name=name,
        enabled=enabled,
        pin_version=table.get("pin_version"),
        setup=table.get("setup"),
        tiers=tiers,
        flags=dict(flags),
    )


def _parse_tool(name: str, raw: Any, file: Path) -> ToolConfig:
    table = _require_table(raw, f"tools.{name}", file)
    check = table.get("check")
    if not isinstance(check, str) or not check:
        _fail(f"tools.{name} requires a string 'check' command", file)
    return ToolConfig(
        name=name,
        check=check,
        install=table.get("install"),
        needed_by=list(table.get("needed_by", [])),
    )


def _parse_secret(name: str, raw: Any, file: Path) -> SecretConfig:
    table = _require_table(raw, f"secrets.{name}", file)
    return SecretConfig(name=name, needed_by=list(table.get("needed_by", [])))


def load_config(workspace_dir: Path) -> Config:
    """Load ``<workspace_dir>/cairn.toml`` into a typed Config.

    Raises ConfigError (naming the offending key) on a missing file, malformed TOML,
    or malformed values; unknown top-level tables become warning Findings on the result.
    """
    file = Path(workspace_dir) / "cairn.toml"
    if not file.is_file():
        message = f"no cairn.toml found in {workspace_dir}"
        raise ConfigError(
            message, findings=[Finding("error", message)], file=str(file)
        )

    try:
        raw = tomllib.loads(file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        message = f"cairn.toml is not valid TOML: {exc}"
        raise ConfigError(
            message, findings=[Finding("error", message)], file=str(file)
        ) from exc

    warnings = [
        Finding("warning", f"unknown top-level key [{key}] in cairn.toml — ignored")
        for key in raw
        if key not in _KNOWN_TOP_LEVEL
    ]

    executors = {
        name: _parse_executor(name, spec, file, warnings)
        for name, spec in _require_table(raw.get("executors", {}), "executors", file).items()
    }
    tools = {
        name: _parse_tool(name, spec, file)
        for name, spec in _require_table(raw.get("tools", {}), "tools", file).items()
    }
    secrets = {
        name: _parse_secret(name, spec, file)
        for name, spec in _require_table(raw.get("secrets", {}), "secrets", file).items()
    }
    sinks = {
        name: dict(_require_table(spec, f"sinks.{name}", file))
        for name, spec in _require_table(raw.get("sinks", {}), "sinks", file).items()
    }

    requires = raw.get("requires")
    if requires is not None and not isinstance(requires, str):
        _fail(
            f"requires must be a version-spec string like '>=0.1,<0.2', got {requires!r}",
            file,
        )

    return Config(
        workspace=_parse_workspace(raw.get("workspace", {}), file, warnings),
        defaults=_parse_defaults(raw.get("defaults", {}), file, warnings),
        executors=executors,
        tools=tools,
        secrets=secrets,
        sinks=sinks,
        requires=requires,
        warnings=warnings,
    )
