"""The workspace test layer — the four L1 suites + ``record`` (TESTING.md §4-5).

``cairn test`` is the workspace author's zero-token confidence check: are the *validators*,
*guards*, *envelopes*, and *pipeline wiring* correct before a single model token is spent?
This module is the library behind it; the CLI binds ``cairn test`` → :func:`run_all` and
``cairn test record`` → :func:`record_run`. Everything here is pure kernel + stdlib and runs
in throwaway temp dirs, so it is safe to call from CI.

The four suites (each returns a :class:`SuiteResult`; a missing ``tests/`` dir is legal and
passes with a ``(no fixtures)`` note — day-0 workspaces have no fixtures yet):

* **validators** — every ``tests/fixtures/<artifact>/`` fixture: ``valid-*`` must pass;
  ``invalid-*`` must fail *with ≥1 reason* (a reasonless rejection is itself a failure).
* **guards** — every ``tests/guards/<guard>/`` payload: ``allow-*`` allowed, ``deny-*``
  denied *with a reason*.
* **pipelines** — every ``tests/matrix.yaml`` row: plan → bootstrap → walk through the
  :class:`StubExecutor` (impersonating every agent executor) in a temp run dir; the row
  passes iff the exit code matches (``ExitCode.OK`` unless ``_expect:`` says otherwise) and,
  on OK, every produced artifact of a done step validates.
* **envelopes** — every agent step's composed six-block envelope, diffed against
  ``tests/envelopes/<pipeline>.<step>.golden.md`` (``update=True`` refreshes). Frozen
  ``now`` + empty trail make it deterministic.

:func:`record_run` harvests a *completed* real run into ``tests/stubs`` + ``tests/fixtures``
so the first real run of a new pipeline regression-locks its wiring forever at zero tokens.
"""

from __future__ import annotations

import difflib
import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import yaml

from cairn.executors.shell import ShellExecutor
from cairn.executors.stub import StubExecutor
from cairn.kernel.artifacts import (
    ArtifactDecl,
    exists,
    resolve_path,
    validate,
)
from cairn.kernel.compose import make_composer, render_artifact_path
from cairn.kernel.config import load_config
from cairn.kernel.errors import CairnError
from cairn.kernel.guards import run_check
from cairn.kernel.plan import (
    LoopNode,
    ParallelNode,
    Plan,
    StepNode,
    plan as build_plan,
)
from cairn.kernel.runstate import load_run
from cairn.kernel.types import ExitCode
from cairn.kernel.walk import bootstrap_run, walk

# A frozen clock for validator/guard/pipeline suites (path templates that use {date} render
# identically every run) and — separately — for the envelope suite's byte-stable goldens.
_NOW = datetime(2026, 1, 1)
_ENVELOPE_NOW = datetime(2026, 1, 1)
# A fixed synthetic run dir for envelope composition: never created, so the trail is empty;
# using a constant keeps the run-dir portion of a golden portable across machines.
_GOLDEN_RUN_DIR = Path("/cairn/run")

_SUITE_NAMES = ("validators", "guards", "pipelines", "envelopes")


# --------------------------------------------------------------------------- #
# Result types.
# --------------------------------------------------------------------------- #


@dataclass
class SuiteResult:
    """One suite's tally. ``notes`` are non-fatal observations (skips, ambiguities, an empty
    ``(no fixtures)`` day-0 marker); ``failures`` are the human-readable failure lines."""

    name: str
    passed: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


@dataclass
class TestReport:
    """The result of :func:`run_all` — every suite's result plus the aggregate verdict."""

    suites: dict[str, SuiteResult]
    ok: bool


# --------------------------------------------------------------------------- #
# Shared helpers.
# --------------------------------------------------------------------------- #


def _pipeline_names(workspace_dir: Path) -> list[str]:
    d = Path(workspace_dir) / "pipelines"
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.yaml"))


def _iter_steps(nodes, in_loop: bool = False) -> Iterator[tuple[StepNode, bool]]:
    """Yield every ``(StepNode, in_loop)`` in a plan, recursing into parallels and loops."""
    for node in nodes:
        if isinstance(node, StepNode):
            yield node, in_loop
        elif isinstance(node, ParallelNode):
            yield from _iter_steps(node.steps, in_loop)
        elif isinstance(node, LoopNode):
            yield from _iter_steps(node.body, True)
        # GateNode: not a step — skipped.


def _plan_defaults(workspace_dir: Path, name: str, overrides: dict[str, str] | None = None) -> Plan:
    return build_plan(workspace_dir, name, overrides or {}, now=_NOW, headless=True)


def _param_str(value: Any) -> str:
    """Params/gate choices are compared as strings; stringify a matrix value for the planner.

    (Quote YAML-boolean enum values in ``matrix.yaml`` — an unquoted ``on`` is parsed as
    ``True`` and stringifies to ``"True"``, which won't match an ``"on"`` enum value.)
    """
    return value if isinstance(value, str) else str(value)


# --------------------------------------------------------------------------- #
# Suite 1 — validators (TESTING §4.1).
# --------------------------------------------------------------------------- #


def run_validator_suite(workspace_dir: Path) -> SuiteResult:
    """valid-* fixtures pass, invalid-* fail with a reason — across every artifact's fixtures."""
    workspace_dir = Path(workspace_dir)
    result = SuiteResult("validators")
    fixtures_root = workspace_dir / "tests" / "fixtures"
    if not fixtures_root.is_dir():
        result.notes.append("(no fixtures)")
        return result

    index = _artifact_index(workspace_dir, result.notes)

    for adir in sorted(p for p in fixtures_root.iterdir() if p.is_dir()):
        aname = adir.name
        if aname not in index:
            result.notes.append(f"fixtures/{aname}: no pipeline declares this artifact — skipped")
            continue
        decl, params, dims, pipeline = index[aname]
        if "*" in decl.path:
            result.notes.append(f"fixtures/{aname}: glob artifact — a single fixture cannot stand in; skipped")
            continue
        for fx in sorted(p for p in adir.iterdir() if p.is_file()):
            if fx.name.startswith("valid-"):
                expect_valid = True
            elif fx.name.startswith("invalid-"):
                expect_valid = False
            else:
                continue  # not a fixture (README, sidecar) — ignore
            ok, reasons = _validate_fixture(workspace_dir, decl, params, dims, pipeline, fx)
            _tally_validator(result, aname, fx.name, expect_valid, ok, reasons)
    return result


def _tally_validator(result: SuiteResult, aname: str, fname: str, expect_valid: bool, ok: bool, reasons: list[str]) -> None:
    where = f"{aname}/{fname}"
    if expect_valid:
        if ok:
            result.passed += 1
        else:
            result.failed += 1
            result.failures.append(f"{where}: expected VALID but was rejected: {reasons}")
    else:
        if ok:
            result.failed += 1
            result.failures.append(f"{where}: expected INVALID but passed validation")
        elif not reasons:
            result.failed += 1
            result.failures.append(f"{where}: rejected without a reason (a reasonless rejection is a failure)")
        else:
            result.passed += 1


def _artifact_index(
    workspace_dir: Path, notes: list[str]
) -> dict[str, tuple[ArtifactDecl, dict, dict, str]]:
    """Map artifact name → (decl, params, dims, pipeline) from the FIRST pipeline that declares it."""
    index: dict[str, tuple[ArtifactDecl, dict, dict, str]] = {}
    for name in _pipeline_names(workspace_dir):
        try:
            p = _plan_defaults(workspace_dir, name)
        except CairnError as exc:
            notes.append(f"pipeline {name!r} did not plan (skipped for the artifact index): {exc}")
            continue
        for aname, decl in p.artifacts.items():
            if aname in index:
                notes.append(f"artifact {aname!r} declared by multiple pipelines; using {index[aname][3]!r}")
                continue
            index[aname] = (decl, p.params, p.dims, p.pipeline)
    return index


def _validate_fixture(
    workspace_dir: Path, decl: ArtifactDecl, params: dict, dims: dict, pipeline: str, fixture: Path
) -> tuple[bool, list[str]]:
    """Copy ``fixture`` to a temp run dir at the decl's rendered path (cycle=1) then validate."""
    rendered = render_artifact_path(decl, params=params, dims=dims, pipeline=pipeline, cycle=1, now=_NOW)
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        dest = run_dir / rendered
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(fixture.read_bytes())
        resolved = resolve_path(decl, rendered, run_dir)
        res = validate(resolved, decl, run_dir, workspace_dir)
        return res.ok, res.reasons


# --------------------------------------------------------------------------- #
# Suite 2 — guards (TESTING §4.2).
# --------------------------------------------------------------------------- #


def run_guard_suite(workspace_dir: Path) -> SuiteResult:
    """allow-* payloads are allowed, deny-* payloads are denied with a reason."""
    workspace_dir = Path(workspace_dir)
    result = SuiteResult("guards")
    guards_root = workspace_dir / "tests" / "guards"
    if not guards_root.is_dir():
        result.notes.append("(no fixtures)")
        return result

    guards = _guard_index(workspace_dir, result.notes)

    for gdir in sorted(p for p in guards_root.iterdir() if p.is_dir()):
        gname = gdir.name
        guard = guards.get(gname)
        if guard is None:
            result.notes.append(f"guards/{gname}: no pipeline declares this guard — skipped")
            continue
        for fx in sorted(gdir.glob("*.json")):
            if fx.name.startswith("allow-"):
                expect_allow = True
            elif fx.name.startswith("deny-"):
                expect_allow = False
            else:
                continue
            allowed, reason = _run_guard_fixture(workspace_dir, guard, json.loads(fx.read_text(encoding="utf-8")))
            _tally_guard(result, gname, fx.name, expect_allow, allowed, reason)
    return result


def _tally_guard(result: SuiteResult, gname: str, fname: str, expect_allow: bool, allowed: bool, reason: str | None) -> None:
    where = f"{gname}/{fname}"
    if expect_allow:
        if allowed:
            result.passed += 1
        else:
            result.failed += 1
            result.failures.append(f"{where}: expected ALLOW but was denied: {reason}")
    else:
        if allowed:
            result.failed += 1
            result.failures.append(f"{where}: expected DENY but was allowed")
        elif not reason:
            result.failed += 1
            result.failures.append(f"{where}: denied without a reason (deny must carry a reason)")
        else:
            result.passed += 1


def _guard_index(workspace_dir: Path, notes: list[str]) -> dict:
    guards: dict = {}
    for name in _pipeline_names(workspace_dir):
        try:
            p = _plan_defaults(workspace_dir, name)
        except CairnError as exc:
            notes.append(f"pipeline {name!r} did not plan (skipped for the guard index): {exc}")
            continue
        for g in p.guards:
            guards.setdefault(g.name, g)
    return guards


def _run_guard_fixture(workspace_dir: Path, guard, payload: dict) -> tuple[bool, str | None]:
    """Run the guard's check against a fixture payload (command/env/run_dir override defaults)."""
    command = payload.get("command", "")
    env = payload.get("env", {}) or {}
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(payload.get("run_dir") or td)
        res = run_check(guard, command=command, env=env, run_dir=run_dir, workspace_dir=workspace_dir)
    return res.allowed, res.reason


# --------------------------------------------------------------------------- #
# Suite 3 — pipelines / stub runs (TESTING §4.3).
# --------------------------------------------------------------------------- #


def run_pipeline_suite(workspace_dir: Path, *, matrix_path: Path | None = None) -> SuiteResult:
    """Run every matrix row through the stub executor; the row passes iff exit + artifacts match."""
    workspace_dir = Path(workspace_dir)
    result = SuiteResult("pipelines")
    matrix_path = Path(matrix_path) if matrix_path is not None else workspace_dir / "tests" / "matrix.yaml"
    if not matrix_path.is_file():
        result.notes.append("(no fixtures)")
        return result

    matrix = yaml.safe_load(matrix_path.read_text(encoding="utf-8")) or {}
    for pipeline_name, rows in matrix.items():
        for i, row in enumerate(rows or []):
            row = row or {}
            label = f"{pipeline_name}[{i}]"
            expect = int(row.get("_expect", int(ExitCode.OK)))
            gates = {k: _param_str(v) for k, v in (row.get("_gates") or {}).items()}
            overrides = {k: _param_str(v) for k, v in row.items() if not k.startswith("_")}
            try:
                why = _run_matrix_row(workspace_dir, pipeline_name, overrides, gates, expect)
            except CairnError as exc:
                result.failed += 1
                result.failures.append(f"{label}: did not plan/run: {exc}")
                continue
            if why is None:
                result.passed += 1
            else:
                result.failed += 1
                result.failures.append(f"{label}: {why}")
    return result


def _run_matrix_row(
    workspace_dir: Path, pipeline_name: str, overrides: dict, gates: dict, expect: int
) -> str | None:
    """Plan → bootstrap → walk one row through the stub. Returns None on pass, else the reason."""
    config = load_config(workspace_dir)
    p = _plan_defaults(workspace_dir, pipeline_name, overrides)

    # One stub instance impersonates EVERY agent executor (claude/codex/grok) in the plan, so
    # agent steps replay canned artifacts; shell steps still use the real shell executor.
    stub = StubExecutor()
    executors: dict[str, Any] = {"shell": ShellExecutor(), "stub": stub}
    for exec_name, _model, _effort in p.resolved_models.values():
        executors[exec_name] = stub

    composer = make_composer(workspace_dir=workspace_dir, config=config, now=_NOW)
    with tempfile.TemporaryDirectory() as td:
        run_dir = bootstrap_run(workspace_dir, p, now=_NOW, runs_root=Path(td))
        code = walk(
            p, run_dir,
            workspace_dir=workspace_dir, config=config, executors=executors,
            composer=composer, interactive=False, gate_presets=gates, now=_NOW,
        )
        if int(code) != expect:
            return f"exit {int(code)} != expected {expect}"
        if expect == int(ExitCode.OK):
            bad = _validate_done_artifacts(p, run_dir, workspace_dir)
            if bad:
                return f"produced artifacts failed validation: {bad}"
    return None


def _validate_done_artifacts(p: Plan, run_dir: Path, workspace_dir: Path) -> list[str]:
    """Re-validate every produced artifact of a done step (belt-and-suspenders over walk)."""
    nodes = load_run(run_dir).get("nodes", {})
    bad: list[str] = []
    for step, _in_loop in _iter_steps(p.nodes):
        entry = nodes.get(step.id)
        if not entry or entry.get("status") != "done":
            continue
        cycle = entry.get("cycles")
        for name in step.produces:
            decl = p.artifacts.get(name)
            if decl is None:
                continue
            use_cycle = cycle if "{cycle}" in decl.path else None
            rendered = render_artifact_path(
                decl, params=p.params, dims=p.dims, pipeline=p.pipeline, cycle=use_cycle, now=_NOW
            )
            res = validate(resolve_path(decl, rendered, run_dir), decl, run_dir, workspace_dir)
            if not res.ok:
                bad.extend(res.reasons)
    return bad


# --------------------------------------------------------------------------- #
# Suite 4 — envelopes (TESTING §4.4).
# --------------------------------------------------------------------------- #


def run_envelope_suite(workspace_dir: Path, *, update: bool = False) -> SuiteResult:
    """Diff each agent step's composed envelope against its golden (``update`` refreshes)."""
    workspace_dir = Path(workspace_dir)
    result = SuiteResult("envelopes")
    env_root = workspace_dir / "tests" / "envelopes"
    # Goldens are opt-in: a workspace with no tests/envelopes dir yet is day-0-legal. Once the
    # dir exists, every agent step must have its golden (a missing one is a coverage gap).
    if not update and not env_root.is_dir():
        result.notes.append("(no fixtures)")
        return result

    config = load_config(workspace_dir)
    composer = make_composer(workspace_dir=workspace_dir, config=config, now=_ENVELOPE_NOW)
    for name in _pipeline_names(workspace_dir):
        try:
            p = build_plan(workspace_dir, name, {}, now=_ENVELOPE_NOW, headless=True)
        except CairnError as exc:
            result.notes.append(f"pipeline {name!r} did not plan (skipped): {exc}")
            continue
        for step, in_loop in _iter_steps(p.nodes):
            if step.kind != "agent":
                continue
            envelope = composer(step, p, _GOLDEN_RUN_DIR, cycle=1 if in_loop else None, retry_reasons=[])
            golden = env_root / f"{name}.{step.id}.golden.md"
            _tally_envelope(result, name, step.id, golden, _portable(envelope, workspace_dir), update)
    return result


def _portable(envelope: str, workspace_dir: Path) -> str:
    """Normalize machine-specific absolute paths to stable tokens so a committed golden
    diffs clean on any checkout (runtime envelopes stay absolute — this is a test-layer
    normalization only, applied identically on write and on compare)."""
    return envelope.replace(str(workspace_dir), "<WORKSPACE>").replace(str(_GOLDEN_RUN_DIR), "<RUN_DIR>")


def _tally_envelope(result: SuiteResult, pipeline: str, step_id: str, golden: Path, envelope: str, update: bool) -> None:
    where = f"{pipeline}.{step_id}"
    if update:
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(envelope, encoding="utf-8")
        result.passed += 1
        return
    if not golden.is_file():
        result.failed += 1
        result.failures.append(f"{where}: no golden yet — run the envelope suite with update=True")
        return
    current = golden.read_text(encoding="utf-8")
    if current == envelope:
        result.passed += 1
    else:
        diff = "".join(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                envelope.splitlines(keepends=True),
                fromfile=f"{golden.name} (golden)",
                tofile=f"{where} (composed)",
            )
        )
        result.failed += 1
        result.failures.append(f"{where}: envelope drift:\n{diff}")


# --------------------------------------------------------------------------- #
# run_all — the four suites under one call (what `cairn test` binds to).
# --------------------------------------------------------------------------- #


def run_all(workspace_dir: Path, suites: list[str] | None = None, *, update: bool = False) -> TestReport:
    """Run the requested suites (default: all four) and return the aggregate report."""
    workspace_dir = Path(workspace_dir)
    names = suites or list(_SUITE_NAMES)
    runners = {
        "validators": lambda: run_validator_suite(workspace_dir),
        "guards": lambda: run_guard_suite(workspace_dir),
        "pipelines": lambda: run_pipeline_suite(workspace_dir),
        "envelopes": lambda: run_envelope_suite(workspace_dir, update=update),
    }
    results: dict[str, SuiteResult] = {}
    for n in names:
        if n not in runners:
            raise ValueError(f"unknown suite {n!r} (valid: {', '.join(_SUITE_NAMES)})")
        results[n] = runners[n]()
    ok = all(r.failed == 0 for r in results.values())
    return TestReport(suites=results, ok=ok)


# --------------------------------------------------------------------------- #
# record — harvest a completed run into tests/stubs + tests/fixtures (TESTING §5).
# --------------------------------------------------------------------------- #


def record_run(workspace_dir: Path, run_dir: Path, *, slim: bool = False) -> list[Path]:
    """Harvest a COMPLETED run into ``tests/stubs`` (+ single-JSON copies into ``tests/fixtures``).

    Re-plans from ``run.json``'s params to map each declared artifact to its producing step,
    copies the artifact's files into ``tests/stubs/<pipeline>/<step>[.c<cycle>]/<rel path>``
    (cycle dirs for loop-body artifacts), and drops each single-``.json`` artifact into
    ``tests/fixtures/<artifact>/valid-recorded.json``. ``slim`` truncates files >64 KiB to a
    marker line (validators must not depend on payload weight). Returns the files created.
    """
    workspace_dir = Path(workspace_dir)
    run_dir = Path(run_dir)
    run_doc = load_run(run_dir)
    pipeline = run_doc["pipeline"]
    params = run_doc.get("params") or {}
    now = _parse_at(run_doc.get("created_at"))

    p = build_plan(workspace_dir, pipeline, {k: _param_str(v) for k, v in params.items()}, now=now)

    producer: dict[str, str] = {}
    for step, _in_loop in _iter_steps(p.nodes):
        for name in step.produces:
            producer.setdefault(name, step.id)

    stubs_pdir = workspace_dir / "tests" / "stubs" / pipeline
    fixtures_root = workspace_dir / "tests" / "fixtures"
    created: list[Path] = []

    for name, decl in p.artifacts.items():
        step_id = producer.get(name)
        if step_id is None:
            continue  # a declared-but-unproduced artifact has nothing to record
        if "{cycle}" in decl.path:
            cyc = 1
            while True:
                rendered = render_artifact_path(decl, params=p.params, dims=p.dims, pipeline=p.pipeline, cycle=cyc, now=now)
                resolved = resolve_path(decl, rendered, run_dir)
                if not exists(resolved):
                    break
                created += _copy_into(resolved, run_dir, stubs_pdir / f"{step_id}.c{cyc}", slim)
                cyc += 1
        else:
            rendered = render_artifact_path(decl, params=p.params, dims=p.dims, pipeline=p.pipeline, cycle=None, now=now)
            resolved = resolve_path(decl, rendered, run_dir)
            if not exists(resolved):
                continue
            created += _copy_into(resolved, run_dir, stubs_pdir / step_id, slim)
            created += _drop_fixture(resolved, fixtures_root / name)

    return created


def _copy_into(resolved, run_dir: Path, dest_dir: Path, slim: bool) -> list[Path]:
    """Copy every file of a resolved artifact into ``dest_dir`` at its run-dir-relative path."""
    out: list[Path] = []
    for src in resolved.paths:
        if not src.is_file():
            continue
        dest = dest_dir / src.relative_to(run_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if slim and src.stat().st_size > 64 * 1024:
            dest.write_text(f"cairn-stub-slim: {src.stat().st_size} bytes truncated\n", encoding="utf-8")
        else:
            dest.write_bytes(src.read_bytes())
        out.append(dest)
    return out


def _drop_fixture(resolved, fixtures_dir: Path) -> list[Path]:
    """A single-``.json`` artifact becomes a ``valid-recorded.json`` validator fixture."""
    json_files = [p for p in resolved.paths if p.suffix == ".json" and p.is_file()]
    if len(resolved.paths) != 1 or len(json_files) != 1:
        return []
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    dest = fixtures_dir / "valid-recorded.json"
    dest.write_bytes(json_files[0].read_bytes())
    return [dest]


def _parse_at(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return _NOW
