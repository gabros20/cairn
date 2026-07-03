"""``cairn new`` — workspace + single-file scaffolding (docs/DISTRIBUTION.md §4).

``new workspace`` instantiates the packaged ``templates/workspace/`` tree (the one hard
requirement: ``cairn run hello`` works immediately, offline, zero auth) with
``{{WORKSPACE_NAME}}`` substituted. ``new pipeline|agent|skill|validator`` drop a minimal,
plan-valid single-file stub into the right workspace directory. Pure stdlib.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_WORKSPACE_MARKER = "{{WORKSPACE_NAME}}"


def templates_dir() -> Path:
    """Locate the packaged ``templates/workspace`` tree.

    Installed wheels force-include the tree at ``cairn/_templates/workspace`` (pyproject), so
    prefer that; fall back to the repo checkout layout (``<repo>/templates/workspace``, where
    this file is ``cairn/kernel/``) for dev. The source tree keeps ``templates/workspace`` at
    the repo root — ``test_scaffold_template.py`` depends on that location.
    """
    try:  # installed: templates force-included inside the package
        from importlib.resources import files

        packaged = Path(str(files("cairn"))) / "_templates" / "workspace"
        if packaged.is_dir():
            return packaged
    except (ModuleNotFoundError, TypeError):  # pragma: no cover - defensive
        pass
    # Repo checkout: cairn/kernel/newkit.py → parents[2] is the repo root.
    checkout = Path(__file__).resolve().parents[2] / "templates" / "workspace"
    if checkout.is_dir():
        return checkout
    raise FileNotFoundError("cannot locate templates/workspace (not in package or checkout)")


def new_workspace(name: str, dest_dir: Path | None = None) -> Path:
    """Instantiate the scaffold at ``<dest_dir>/<name>`` (or ``./<name>``); return the dir.

    Copies the template tree verbatim then substitutes ``{{WORKSPACE_NAME}}`` → ``name`` in
    every text file. Refuses to overwrite an existing directory.
    """
    src = templates_dir()
    dest = (Path(dest_dir) / name) if dest_dir else Path(name)
    if dest.exists():
        raise FileExistsError(f"{dest} already exists")
    shutil.copytree(src, dest)
    for path in dest.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary / unreadable — leave untouched
        if _WORKSPACE_MARKER in text:
            path.write_text(text.replace(_WORKSPACE_MARKER, name), encoding="utf-8")
    return dest


# --------------------------------------------------------------------------- #
# Single-file stubs — tiny + plan-valid.
# --------------------------------------------------------------------------- #

_PIPELINE_STUB = """\
# {name} — a new pipeline. Plans green as-is; grow it with real steps.
# Reference: docs/API.md §2.
pipeline: {name}
version: 1

run_id: "{name}-{{date}}"

steps:
  - id: hello
    run: "echo 'hello from {name}'"
"""

_AGENT_STUB = """\
# {name} — a new agent. Referenced by an `agent: {name}` step.
description: "TODO: what this agent does"
tier: balanced          # reasoning | balanced | cheap
effort: medium          # low | medium | high | xhigh
skills: []
tools:
  allow: [read, write, edit, bash]
"""

_SKILL_STUB = """\
---
name: {name}
description: TODO — one line describing when to use this skill.
---

# {name}

TODO: the skill body, inlined verbatim into every agent envelope that declares it.
"""

_VALIDATOR_STUB = """\
#!/usr/bin/env python3
\"\"\"{name} — an artifact validator. argv: run_dir, artifact_name, artifact_path.

Exit 0 to pass; exit 1 and print one reason per line to fail.
\"\"\"
import sys


def main() -> int:
    _run_dir, _name, _path = sys.argv[1], sys.argv[2], sys.argv[3]
    # TODO: inspect the artifact at _path; print reasons + return 1 to reject.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def new_stub(kind: str, name: str, workspace_dir: Path) -> Path:
    """Write a minimal ``kind`` stub for ``name`` into ``workspace_dir``; return the file.

    ``kind`` ∈ {pipeline, agent, skill, validator}. Refuses to overwrite an existing file.
    """
    workspace_dir = Path(workspace_dir)
    if kind == "pipeline":
        path = workspace_dir / "pipelines" / f"{name}.yaml"
        body = _PIPELINE_STUB.format(name=name)
    elif kind == "agent":
        path = workspace_dir / "agents" / f"{name}.yaml"
        body = _AGENT_STUB.format(name=name)
    elif kind == "skill":
        path = workspace_dir / "skills" / name / "SKILL.md"
        body = _SKILL_STUB.format(name=name)
    elif kind == "validator":
        path = workspace_dir / "validators" / f"{name}.py"
        body = _VALIDATOR_STUB.format(name=name)
    else:
        raise ValueError(f"unknown new target {kind!r}")

    if path.exists():
        raise FileExistsError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    if kind == "validator":
        path.chmod(0o755)
    return path
