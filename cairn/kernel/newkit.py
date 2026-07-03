"""``cairn new`` — workspace + single-file scaffolding (docs/DISTRIBUTION.md §4).

``new workspace`` instantiates the packaged ``templates/workspace/`` tree (the one hard
requirement: ``cairn run hello`` works immediately, offline, zero auth) with
``{{WORKSPACE_NAME}}`` substituted. ``new pipeline|agent|skill|validator`` drop a minimal,
plan-valid single-file stub into the right workspace directory. Stdlib + pyyaml (the
matrix-append check on the packaged self-improve retrofit).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

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
# Packaged pipelines — furniture copied from the template, not stubbed.
#
# `cairn new pipeline self-improve` retrofits the learning-loop furniture
# (docs/TOOLING-AND-GROWTH.md §7) into an EXISTING workspace: the pipeline plus its
# curator agent, curation-doctrine skill, proposals schema + validator, open-pr
# script, and test fixtures/stubs/matrix. The pipeline file itself refuses to
# overwrite; companion files that already exist are left untouched (never clobber a
# customization) — EXCEPT tests/matrix.yaml, which is a shared aggregation file: when
# it exists without a top-level `self-improve:` key, the packaged row is APPENDED
# (append-only; existing rows are never touched), so the standing headless-refusal
# proof isn't silently dropped. cairn.toml is never edited — the pipeline's header
# comment names the two blocks ([executors.*] tiers, [tools.gh]) an older workspace
# wires by hand.
# --------------------------------------------------------------------------- #

_PACKAGED_PIPELINE_FILES: dict[str, tuple[str, ...]] = {
    "self-improve": (
        "pipelines/self-improve.yaml",
        "agents/curator.yaml",
        "skills/self-improve-curator/SKILL.md",
        "schemas/self-improve-proposals.json",
        "validators/self-improve-proposals.py",
        "scripts/self-improve-open-pr.py",
        "tests/fixtures/proposals/valid-two-promotions.json",
        "tests/fixtures/proposals/invalid-target-escapes.json",
        "tests/fixtures/proposals/invalid-missing-rationale.json",
        "tests/stubs/self-improve/curate/proposals.json",
        "tests/matrix.yaml",
    ),
}


_MATRIX_REL = "tests/matrix.yaml"

# Appended verbatim to an existing matrix that lacks the row (see the note above).
_MATRIX_SELF_IMPROVE_ROW = """\

self-improve:
  # Headless, no presets: the approve gate resolves to its DEFAULT — "no" — so
  # open-pr is SKIPPED: the standing proof an unattended run can never self-promote.
  - {}
"""


def _append_matrix_row(dest: Path) -> None:
    """Append the packaged ``self-improve:`` row to an existing matrix that lacks it.

    Append-only by design: existing rows are never rewritten. A matrix that already
    carries a top-level ``self-improve`` key (or that doesn't parse as a mapping —
    the workspace's own `cairn test` will say so far more precisely) is left alone.
    """
    text = dest.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return
    if not isinstance(doc, dict) or "self-improve" in doc:
        return
    joiner = "" if text.endswith("\n") else "\n"
    dest.write_text(text + joiner + _MATRIX_SELF_IMPROVE_ROW, encoding="utf-8")


def _copy_packaged_pipeline(name: str, workspace_dir: Path) -> Path:
    src = templates_dir()
    pipeline_rel = f"pipelines/{name}.yaml"
    pipeline_path = workspace_dir / pipeline_rel
    if pipeline_path.exists():
        raise FileExistsError(f"{pipeline_path} already exists")
    for rel in _PACKAGED_PIPELINE_FILES[name]:
        dest = workspace_dir / rel
        if dest.exists() and rel != pipeline_rel:
            if rel == _MATRIX_REL:
                _append_matrix_row(dest)  # shared file: append the row, touch nothing else
            continue  # keep the workspace's customized copy
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, dest)  # copy2 keeps the exec bit on validators/scripts
    return pipeline_path


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
    if kind == "pipeline" and name in _PACKAGED_PIPELINE_FILES:
        return _copy_packaged_pipeline(name, workspace_dir)
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
