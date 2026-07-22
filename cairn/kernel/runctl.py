"""RunController — the run/resume entrance as a kernel library (FACTORY-PLAN W0.2).

Extracts the entrance logic that lived in ``cli.py`` (guard sequence, tool preflight,
mint-vs-resume resolution) so CLI, the future queue drain, and ``cairn inbox`` can all
call the same paths as functions. Typed refusals and advisories replace printed exit
codes and stderr side effects at the core — the CLI owns presentation (prints
``Refusal.message`` / ``advisories``). Behavior is byte-identical at the CLI boundary
(D7). No factory features; no second copy of the guards (D10).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from cairn.executors._cli import _probe_version
from cairn.kernel.config import installed_version, version_compat
from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.plan import Plan
from cairn.kernel.proc import SubprocessRunner
from cairn.kernel.runstate import load_run
from cairn.kernel.schedkit import find_idempotent_run
from cairn.kernel.toolcheck import run_tool_check
from cairn.kernel.types import ExitCode
from cairn.kernel.walk import bootstrap_run


# --------------------------------------------------------------------------- #
# Result types — typed refusals and success shapes for entrance callers.
# --------------------------------------------------------------------------- #


class RefusalKind(str, Enum):
    """Why an entrance refused — discriminant for inbox cards / drain routing.

    All four kinds map to ``ExitCode.CONFIG`` today; ``kind`` is what callers
    switch on (not message substring matching).
    """

    TOOLS = "tools"
    DRIFT = "drift"
    VERSION = "version"
    UNREADABLE_RUN = "unreadable_run"


@dataclass(frozen=True)
class Refusal:
    """A hard stop: the entrance will not mint or resume.

    ``message`` is the full stderr text (verbatim, including any remedy sentence).
    ``kind`` discriminates the four refusal paths for library consumers.
    ``remedy`` is the structured escape-hatch fragment when one exists (e.g. the
    ``cairn resume <dir> --force`` guidance), else None — for library consumers;
    the CLI prints ``message`` alone so output stays byte-identical.
    """

    code: ExitCode
    message: str
    kind: RefusalKind
    remedy: str | None = None


@dataclass(frozen=True)
class Minted:
    """A fresh run dir was created (``bootstrap_run`` succeeded after preflight)."""

    run_dir: Path
    advisories: tuple[str, ...] = ()


@dataclass(frozen=True)
class Resumable:
    """An existing run dir passed the resume guards (drift → version → repro).

    ``advisories`` are stderr warning lines (force-override, version drift,
    reproducibility) collected in print order — the CLI prints each; runctl
    never writes to stderr.
    """

    run_dir: Path
    run_doc: dict[str, Any]
    advisories: tuple[str, ...] = ()


@dataclass(frozen=True)
class AlreadyDone:
    """``--idempotent`` matched a complete run — no-op, exit 0."""

    run_dir: Path


# --------------------------------------------------------------------------- #
# Tool preflight — hard-stop before mint / walk (docs/TOOLING-AND-GROWTH.md §2).
# --------------------------------------------------------------------------- #


def preflight_tools(p: Plan) -> Refusal | None:
    """Range-scoped tool checks. Nothing on disk is the caller's job to ensure.

    Wired into every entrance about to execute: a fresh mint (before
    ``bootstrap_run``), both resume entrances of ``cairn run``, and ``cairn
    resume`` — always AFTER the resume guards. Only the ``--idempotent``
    complete-match short-circuit skips checks. Returns a :class:`Refusal` with
    the multi-line CONFIG message, or None when all checks pass (zero output).
    """
    failures = [req for req in p.tool_requirements if not run_tool_check(req.check)]
    if not failures:
        return None
    lines = [f"cairn: refusing to run {p.pipeline!r} — required tool(s) unverified on this machine:"]
    for req in failures:
        lines.append(f"  ✗ {req.tool}  `{req.check}` failed (needed by: {', '.join(req.targets)})")
        if req.install:
            lines.append(f"      fix: {req.install}")
    lines.append("  → run `cairn doctor` to verify tooling, then re-run.")
    return Refusal(
        code=ExitCode.CONFIG,
        message="\n".join(lines),
        kind=RefusalKind.TOOLS,
        remedy="run `cairn doctor` to verify tooling, then re-run.",
    )


# --------------------------------------------------------------------------- #
# Resume guards — drift → version → reproducibility (advisory).
# --------------------------------------------------------------------------- #


def _pipeline_drift_guard(
    recorded: str | None,
    current_hash: str,
    pipeline: str,
    run_dir: Path,
    *,
    force: bool,
) -> Refusal | tuple[str, ...]:
    """Pipeline-hash drift: refuse unless ``force``; advisory on override.

    Returns a :class:`Refusal`, or a (possibly empty) tuple of advisory lines.
    """
    if not recorded or recorded in ("sha256:unknown", current_hash):
        return ()
    if not force:
        remedy = (
            f"Run `cairn resume {run_dir} --force` to resume against the current file."
        )
        return Refusal(
            code=ExitCode.CONFIG,
            message=(
                f"cairn: pipeline {pipeline!r} has changed since this run was planned "
                f"(hash drift). {remedy}"
            ),
            kind=RefusalKind.DRIFT,
            remedy=remedy,
        )
    return (
        f"cairn: warning — resuming across pipeline-hash drift (--force) for {pipeline!r}",
    )


def _version_compat_guard(
    recorded: str | None, run_dir: Path, *, force: bool
) -> Refusal | tuple[str, ...]:
    """Cross-version resume gate (docs/DISTRIBUTION.md §3). Cross-major refuses
    without ``--force``; cross-minor / unrecorded yield an advisory and proceed."""
    installed = installed_version()
    verdict = version_compat(recorded, installed)
    if verdict == "ok":
        return ()
    if verdict == "warn":
        if recorded:
            return (
                f"cairn: warning — resuming a run created by cairn {recorded} on cairn "
                f"{installed} (version drift)",
            )
        return (
            f"cairn: warning — this run dir records no cairn version; resuming on cairn "
            f"{installed}",
        )
    # verdict == "refuse" — cross-major.
    if not force:
        remedy = (
            f"Run `cairn resume {run_dir} --force` to resume against the installed version."
        )
        return Refusal(
            code=ExitCode.CONFIG,
            message=(
                f"cairn: this run was created by cairn {recorded} but cairn {installed} is "
                f"installed (major-version drift). {remedy}"
            ),
            kind=RefusalKind.VERSION,
            remedy=remedy,
        )
    return (
        f"cairn: warning — resuming across cairn-version drift (--force): "
        f"run {recorded} vs installed {installed}",
    )


def workspace_git_rev(ws: Path) -> dict[str, Any] | None:
    """Workspace git HEAD + dirty flag, or None when not a repo / git missing.

    Shared by the mint-time reproducibility record and the resume-time drift
    advisory. Never raises — a missing binary/non-repo is absent, not a failure.
    """
    runner = SubprocessRunner()
    try:
        head = runner.run(["git", "-C", str(ws), "rev-parse", "HEAD"])
    except OSError:
        return None
    if head.returncode != 0 or not head.stdout.strip():
        return None
    try:
        status = runner.run(["git", "-C", str(ws), "status", "--porcelain"])
    except OSError:
        status = None
    dirty = bool(status.stdout.strip()) if status is not None and status.returncode == 0 else False
    return {"rev": head.stdout.strip(), "dirty": dirty}


def _reproducibility_drift_guard(recorded: dict, ws: Path) -> tuple[str, ...]:
    """Collect advisories when an executor version or workspace git rev drifted.

    Probe failures are silent (the tool hard-stop / doctor own "can this run");
    this only speaks when a probe succeeds with a disagreeing value. Only
    ``git_rev`` is compared — ``git_dirty`` alone is never warned on.
    """
    advisories: list[str] = []
    recorded_versions = (recorded.get("executors") or {}).get("versions") or {}
    for name, old in sorted(recorded_versions.items()):
        if not old or shutil.which(name) is None:
            continue
        try:
            code, current = _probe_version(name)
        except (OSError, CairnError):
            continue
        if code == 0 and current and current != old:
            advisories.append(
                f"cairn: warning — executor {name!r} reports {current!r} at resume, "
                f"recorded {old!r} at mint (version drift)"
            )

    recorded_git = recorded.get("git_rev")
    if recorded_git:
        current = workspace_git_rev(ws)
        if current is not None and current["rev"] != recorded_git:
            advisories.append(
                f"cairn: warning — workspace is at git {current['rev']} at resume, "
                f"recorded {recorded_git} at mint (workspace drift)"
            )
    return tuple(advisories)


def resume_existing(
    run_dir: Path,
    *,
    ws: Path,
    phash: str,
    pipeline: str,
    force: bool,
) -> Resumable | Refusal:
    """Load the run fail-loud, then drift → version → reproducibility (advisory).

    Order and wording match the former ``cli._resume_guards`` / ``cairn resume``
    gate sequence exactly. Does not preflight tools (caller has the Plan) and
    does not re-pin on ``force`` (CLI owns the repin side effect). Never writes
    to stderr — advisories ride on :class:`Resumable`.
    """
    run_dir = Path(run_dir)
    try:
        recorded = load_run(run_dir)
    except (OSError, ValueError, ConfigError) as exc:
        return Refusal(
            code=ExitCode.CONFIG,
            message=f"cairn: cannot read {run_dir}/run.json: {exc}",
            kind=RefusalKind.UNREADABLE_RUN,
        )
    advisories: list[str] = []
    drift = _pipeline_drift_guard(
        recorded.get("pipeline_hash"), phash, pipeline, run_dir, force=force
    )
    if isinstance(drift, Refusal):
        return drift
    advisories.extend(drift)
    version = _version_compat_guard(recorded.get("cairn_version"), run_dir, force=force)
    if isinstance(version, Refusal):
        return version
    advisories.extend(version)
    advisories.extend(_reproducibility_drift_guard(recorded, ws))
    return Resumable(run_dir=run_dir, run_doc=recorded, advisories=tuple(advisories))


# --------------------------------------------------------------------------- #
# Mint + resolve — the `cairn run` decision tree.
# --------------------------------------------------------------------------- #


def mint_new(
    ws: Path,
    plan: Plan,
    *,
    now: datetime,
    pipeline_hash: str,
    runs_root: Path | None = None,
    run_dir: Path | None = None,
) -> Minted | Refusal:
    """Preflight tools, then ``bootstrap_run``. Nothing on disk when preflight refuses."""
    refused = preflight_tools(plan)
    if refused is not None:
        return refused
    created = bootstrap_run(
        ws,
        plan,
        now=now,
        runs_root=runs_root,
        run_dir=run_dir,
        pipeline_hash=pipeline_hash,
    )
    return Minted(run_dir=created)


def _idempotent_done(run_dir: Path) -> AlreadyDone | None:
    """Fast path for ``--idempotent`` on an existing run dir: done → AlreadyDone."""
    try:
        status = load_run(run_dir).get("status")
    except (OSError, ValueError, ConfigError):
        return None
    if status == "done":
        return AlreadyDone(run_dir=run_dir)
    return None


def resolve_run(
    ws: Path,
    plan: Plan,
    *,
    now: datetime,
    pipeline_hash: str,
    runs_root: Path | None = None,
    run_dir: Path | str | None = None,
    idempotent: bool = False,
) -> Minted | Resumable | AlreadyDone | Refusal:
    """The ``cairn run`` entrance decision tree: --run-dir / --idempotent / fresh.

    Returns a typed result; never prints (AlreadyDone is silent here — the CLI
    prints ``already done →``). Refuse paths carry full messages. Resume paths
    always force=False (``cairn run`` has no --force); preflight runs after
    guards and before returning Resumable, matching the old order.
    """
    if run_dir is not None:
        run_dir = Path(run_dir).resolve()
        existing = (run_dir / "run.json").is_file()
        if existing and idempotent:
            done = _idempotent_done(run_dir)
            if done is not None:
                return done
        if not existing:
            return mint_new(
                ws,
                plan,
                now=now,
                runs_root=runs_root,
                run_dir=run_dir,
                pipeline_hash=pipeline_hash,
            )
        # Existing --run-dir resumes through guards then tool hard-stop.
        result = resume_existing(
            run_dir, ws=ws, phash=pipeline_hash, pipeline=plan.pipeline, force=False
        )
        if isinstance(result, Refusal):
            return result
        refused = preflight_tools(plan)
        if refused is not None:
            return refused
        return result

    # No --run-dir: optional idempotent match, else fresh mint under runs_root.
    match = (
        find_idempotent_run(runs_root, pipeline=plan.pipeline, params=plan.params, now=now)
        if idempotent and runs_root is not None
        else None
    )
    if match is not None and match.complete:
        return AlreadyDone(run_dir=match.run_dir)
    if match is not None:
        result = resume_existing(
            match.run_dir,
            ws=ws,
            phash=pipeline_hash,
            pipeline=plan.pipeline,
            force=False,
        )
        if isinstance(result, Refusal):
            return result
        refused = preflight_tools(plan)
        if refused is not None:
            return refused
        return result
    return mint_new(
        ws,
        plan,
        now=now,
        runs_root=runs_root,
        pipeline_hash=pipeline_hash,
    )
