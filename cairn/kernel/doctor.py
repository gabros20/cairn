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
    planned, lint_errors = 0, 0
    for name in names:
        try:
            build_plan(workspace_dir, name, {}, now=now, headless=True)
            planned += 1
        except ConfigError as exc:
            lint_errors += 1
            errors += 1
            out(f"{_BAD} workspace lint  pipeline {name!r}: {exc}")
    if lint_errors == 0:
        out(f"{_OK} workspace lint  {planned} pipeline{'s' if planned != 1 else ''} plan green")

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
        out("  hook probe: not implemented (C4)")

    return int(ExitCode.CONFIG) if errors else int(ExitCode.OK)


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
