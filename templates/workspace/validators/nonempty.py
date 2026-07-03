#!/usr/bin/env python3
"""Starter validator: the named artifact exists and is non-empty.

Contract (docs/API.md §4): a validator is any executable invoked as

    nonempty.py <run_dir> <artifact_name> <artifact_path>

    exit 0                    → pass
    exit 1 + one reason/line  → fail (each stdout line is a machine-readable reason,
                                fed to the trail, the halt message, and retry envelopes)

`artifact_path` is the artifact's rendered, run-dir-relative path — for a glob
artifact it is the glob *pattern* (e.g. `blueprints/**`). This validator resolves it
under `run_dir`, so it never needs to know the pipeline:

  * plain path   → <run_dir>/<artifact_path> must exist and be non-empty (a directory
                   counts as non-empty when it holds at least one entry).
  * glob pattern → matched under <run_dir>; non-empty ⇔ ≥1 match AND every matched
                   file is non-empty (matched directories are ignored for the size test).

Back-compat: older callers passed only [run_dir, artifact_name] and used the *name* as
the path. If `artifact_path` (argv[3]) is absent, this falls back to treating
`artifact_name` (argv[2]) as the run-dir-relative path.

Pure and side-effect-free — safe to run from the walker, `cairn validate`, a retry
prompt, or CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

_GLOB_CHARS = ("*", "?", "[")


def _check_path(run_dir: Path, rel: str) -> list[str]:
    """Failure reasons for a plain (non-glob) artifact path — empty list = pass."""
    path = run_dir / rel
    if not path.exists():
        return [f"artifact not found: {rel}"]
    if path.is_dir():
        if not any(path.iterdir()):
            return [f"artifact directory is empty: {rel}"]
        return []
    if path.stat().st_size == 0:
        return [f"artifact is empty: {rel}"]
    return []


def _check_glob(run_dir: Path, pattern: str) -> list[str]:
    """Failure reasons for a glob artifact: ≥1 match, every matched file non-empty.

    A trailing ``**`` (as in ``blueprints/**``) is expanded to ``**/*`` so it
    enumerates descendant *files*, not just directories — pathlib's bare ``**``
    matches directories only.
    """
    expanded = f"{pattern}/*" if pattern.endswith("**") else pattern
    matches = sorted(run_dir.glob(expanded))
    if not matches:
        return [f"artifact pattern matched nothing: {pattern}"]
    return [
        f"artifact file is empty: {m.relative_to(run_dir)}"
        for m in matches
        if m.is_file() and m.stat().st_size == 0
    ]


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print("usage: nonempty.py <run_dir> <artifact_name> [<artifact_path>]")
        return 1

    run_dir = Path(argv[0])
    # Prefer the rendered path (argv[3]); fall back to the name for older callers.
    rel = argv[2] if len(argv) == 3 else argv[1]

    reasons = _check_glob(run_dir, rel) if any(c in rel for c in _GLOB_CHARS) else _check_path(run_dir, rel)
    for reason in reasons:
        print(reason)
    return 1 if reasons else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
