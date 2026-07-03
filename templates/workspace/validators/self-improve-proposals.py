#!/usr/bin/env python3
"""self-improve-proposals — semantic checks on the curate step's proposals artifact.

Contract (docs/API.md §4): invoked as

    self-improve-proposals.py <run_dir> <artifact_name> <artifact_path>
    exit 0 → pass; exit 1 + one reason per stdout line → fail

The JSON Schema (schemas/self-improve-proposals.json) owns the SHAPE; this validator
owns what a schema cannot say — that every proposed edit is safely applicable inside
the workspace:

  * the file parses as JSON and carries a `proposals` list;
  * proposal ids are unique;
  * `target` stays inside the workspace: relative, no `..` segments, and never under
    the run/control dirs (`runs/`, `.git/`) or the secrets file (`.env`);
  * `action: replace` carries a non-empty `find`.

Pure and side-effect-free — safe from the walker, `cairn validate`, retries, or CI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path, PurePosixPath

_FORBIDDEN_ROOTS = ("runs", ".git")


def _check_target(i: int, target: str) -> list[str]:
    where = f"proposals[{i}]"
    if not isinstance(target, str) or not target:
        return [f"{where}: target must be a non-empty string"]
    p = PurePosixPath(target)
    if p.is_absolute() or (len(target) > 1 and target[1] == ":"):
        return [f"{where}: target must be workspace-relative, got absolute path {target!r}"]
    if ".." in p.parts:
        return [f"{where}: target escapes the workspace (contains '..'): {target!r}"]
    if p.parts and p.parts[0] in _FORBIDDEN_ROOTS:
        return [f"{where}: target may not touch {p.parts[0]!r}: {target!r}"]
    if target == ".env":
        return [f"{where}: target may not be the secrets file .env"]
    return []


def check(doc: object) -> list[str]:
    """All failure reasons for a parsed proposals document — empty list = pass."""
    if not isinstance(doc, dict) or not isinstance(doc.get("proposals"), list):
        return ["proposals artifact must be an object with a 'proposals' list"]
    reasons: list[str] = []
    seen: set[str] = set()
    for i, prop in enumerate(doc["proposals"]):
        if not isinstance(prop, dict):
            reasons.append(f"proposals[{i}]: must be an object")
            continue
        pid = prop.get("id")
        if isinstance(pid, str) and pid:
            if pid in seen:
                reasons.append(f"proposals[{i}]: duplicate id {pid!r}")
            seen.add(pid)
        reasons.extend(_check_target(i, prop.get("target", "")))
        if prop.get("action") == "replace" and not prop.get("find"):
            reasons.append(f"proposals[{i}]: action 'replace' requires a non-empty 'find'")
    return reasons


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: self-improve-proposals.py <run_dir> <artifact_name> <artifact_path>")
        return 1
    run_dir, _name, rel = Path(argv[0]), argv[1], argv[2]
    path = run_dir / rel
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"proposals artifact unreadable: {exc}")
        return 1
    except json.JSONDecodeError as exc:
        print(f"proposals artifact is not valid JSON: {exc}")
        return 1
    reasons = check(doc)
    for reason in reasons:
        print(reason)
    return 1 if reasons else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
