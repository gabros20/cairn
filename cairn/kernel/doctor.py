"""``cairn doctor`` — the machine preflight (docs/DISTRIBUTION.md §5).

One legible pass over the things that make a run possible on THIS machine: the workspace
lints (config warnings + every pipeline plans), external ``[tools]`` are present/authed,
declared ``[secrets]`` exist by name, the in-scope executors report healthy, and the guard
runner imports on this interpreter. Every failure prints its fix; the exit code is non-zero
only on errors relevant to the requested scope (a scoped tool failure warns, never blocks).

Pure orchestration over the kernel — stdlib + pinned modules only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from cairn.kernel.config import Config, load_config
from cairn.kernel.errors import ConfigError
from cairn.kernel.plan import plan as build_plan

_OK = "✔"
_BAD = "✗"


def _pipeline_names(workspace_dir: Path) -> list[str]:
    d = Path(workspace_dir) / "pipelines"
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.yaml"))


def _required_without_default(workspace_dir: Path, name: str) -> list[str]:
    """Params a pipeline declares ``required: true`` with no ``default`` — the ones that make a
    bare plan impossible. A malformed/unreadable file returns ``[]`` so the real planner reports
    the error precisely."""
    import yaml

    f = Path(workspace_dir) / "pipelines" / f"{name}.yaml"
    try:
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(doc, dict):
        return []
    out = []
    for pname, spec in (doc.get("params") or {}).items():
        spec = spec or {}
        if isinstance(spec, dict) and spec.get("required") and "default" not in spec:
            out.append(pname)
    return out


def run_doctor(
    workspace_dir: Path,
    *,
    executor: str | None = None,
    probe_hooks: bool = False,
    now: datetime | None = None,
    out: Callable[[str], None] = print,
) -> int:
    """Run the doctor over ``workspace_dir``; print findings; return an exit code.

    Non-zero (``ExitCode.CONFIG``) iff an in-scope *error* is found: a workspace lint
    ConfigError or an executor the workspace depends on reporting an error. Tool, secret,
    and guard-runner problems are warnings — they never fail the exit on their own.
    """
    from cairn.kernel.types import ExitCode

    now = now or datetime.now()
    errors = 0

    try:
        config = load_config(workspace_dir)
    except ConfigError as exc:
        out(f"{_BAD} cairn.toml    {exc}")
        return int(ExitCode.CONFIG)

    out(f"{_OK} cairn {_version()}")

    # -- workspace lint: config warnings + every pipeline plans --------------- #
    for w in config.warnings:
        out(f"  ! {w.message}")
    names = _pipeline_names(workspace_dir)
    planned, lint_errors, skipped = 0, 0, 0
    for name in names:
        # A pipeline whose required params have no defaults can't be planned bare — that's a
        # normal shape (it needs `--param`s at run time), not a config error. Skip it with a
        # note so a real workspace stays green; only genuine ConfigErrors fail the lint.
        needs = _required_without_default(workspace_dir, name)
        if needs:
            skipped += 1
            out(f"  ~ pipeline {name!r} skipped: requires params: {', '.join(needs)}")
            continue
        try:
            build_plan(workspace_dir, name, {}, now=now, headless=True)
            planned += 1
        except ConfigError as exc:
            lint_errors += 1
            errors += 1
            out(f"{_BAD} workspace lint  pipeline {name!r}: {exc}")
    if lint_errors == 0:
        note = f" ({skipped} need params)" if skipped else ""
        out(f"{_OK} workspace lint  {planned} pipeline{'s' if planned != 1 else ''} plan green{note}")

    # -- executors: the default + any named ---------------------------------- #
    scope = []
    default = config.workspace.default_executor
    if default:
        scope.append(default)
    if executor and executor not in scope:
        scope.append(executor)
    for name in scope:
        errors += _doctor_executor(config, workspace_dir, name, out)

    # -- tools: presence/auth, scoped by needed_by --------------------------- #
    for name, tool in config.tools.items():
        _doctor_tool(name, tool, out)

    # -- secrets: presence by NAME only (never a value) ---------------------- #
    dotenv = _load_dotenv_names(workspace_dir)
    for name in config.secrets:
        present = name in os.environ or name in dotenv
        mark = _OK if present else _BAD
        out(f"{mark} secret {name}    {'present' if present else 'not set (env or .env)'}")

    # -- guard runner: does the shim-check interpreter import? --------------- #
    if _guard_runner_imports():
        out(f"{_OK} guard runner    cairn.kernel.guards imports")
    else:
        out(f"{_BAD} guard runner    cannot import — guarded commands will fail closed")

    if probe_hooks:
        errors += _doctor_probe_hooks(scope, workspace_dir, out)

    return int(ExitCode.CONFIG) if errors else int(ExitCode.OK)


def _doctor_probe_hooks(scope: list[str], workspace_dir: Path, out: Callable[[str], None]) -> int:
    """Empirically probe hook firing for each in-scope executor (docs/ARCHITECTURE.md §4).

    Spends a token per executor with a recipe, so it runs ONLY under ``--probe-hooks``. Reads
    each executor's asserted ``capabilities.blocking_hooks`` (never writes it) and lets
    :func:`hookprobe.render` decide the level: a probe that *falsifies* a ``True`` claim counts
    toward the non-zero exit; an inconclusive probe only warns."""
    from cairn.cli import load_executor_class
    from cairn.kernel import hookprobe

    errs = 0
    for name in scope:
        recipe = hookprobe.RECIPES.get(name)
        if recipe is None:
            out(f"  ~ hook probe {name}   no probe recipe (skipped)")
            continue
        try:
            blocking_hooks = load_executor_class(name).capabilities.blocking_hooks
        except (KeyError, AttributeError):
            out(f"  ~ hook probe {name}   no such executor plugin (skipped)")
            continue
        result = hookprobe.probe(recipe, workspace_dir=workspace_dir)
        level, line = hookprobe.render(result, blocking_hooks)
        out(line)
        # Side-channel findings (e.g. a canary dir that survived cleanup and may hold copied
        # auth material) — warnings only, never part of the exit policy.
        for w in result.warnings:
            out(f"  ! hook probe {name}   {w}")
        if level == "error":
            errs += 1
    return errs


def _doctor_executor(config: Config, workspace_dir: Path, name: str, out: Callable[[str], None]) -> int:
    # Lazy import: cli imports this module at top level, so we reach back for the shared
    # entry-point registry at call time to avoid a circular import.
    from cairn.cli import build_executor, load_executor_class

    ec = config.executors.get(name)
    try:
        cls = load_executor_class(name)
    except KeyError:
        out(f"{_BAD} executor {name}   no such executor plugin (entry point missing)")
        return 1
    try:
        ex = build_executor(name, cls, ec)
        findings = ex.doctor()
    except Exception as exc:  # noqa: BLE001 - a broken executor must not crash doctor
        out(f"{_BAD} executor {name}   doctor failed: {exc}")
        return 1
    errs = [f for f in findings if f.level == "error"]
    if not errs:
        detail = findings[0].message if findings else "healthy"
        out(f"{_OK} executor {name}   {detail}")
    for f in findings:
        if f.level == "error":
            fix = f" → {f.fix}" if f.fix else ""
            out(f"{_BAD} executor {name}   {f.message}{fix}")
        elif f.level == "warning":
            out(f"  ! executor {name}   {f.message}")
    return 1 if errs else 0


def _doctor_tool(name: str, tool, out: Callable[[str], None]) -> None:
    ok = _run_check(tool.check)
    if ok:
        out(f"{_OK} tool {name}")
        return
    scope = f" (needed by: {', '.join(tool.needed_by)})" if tool.needed_by else ""
    fix = f" → {tool.install}" if tool.install else ""
    out(f"{_BAD} tool {name}       `{tool.check}` failed{fix}{scope}")


def _run_check(check: str) -> bool:
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", check],
            capture_output=True,
            text=True,
            timeout=30,
            env=os.environ.copy(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def _guard_runner_imports() -> bool:
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import cairn.kernel.guards"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def _load_dotenv_names(workspace_dir: Path) -> set[str]:
    path = Path(workspace_dir) / ".env"
    names: set[str] = set()
    if not path.is_file():
        return names
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        names.add(line.split("=", 1)[0].strip())
    return names


def _version() -> str:
    import cairn

    return cairn.__version__
