#!/usr/bin/env python3
"""Starter validator: the named artifact exists and is non-empty.

Contract (docs/API.md §4): a validator is any executable invoked as

    nonempty.py <run_dir> <artifact_name>

    exit 0                    → pass
    exit 1 + one reason/line  → fail (each stdout line is a machine-readable reason,
                                fed to the trail, the halt message, and retry envelopes)

`artifact_name` is interpreted here as a path RELATIVE to `run_dir`, so hello.yaml's
`message` artifact (declared `path: message.txt`) resolves to `<run_dir>/message.txt`.
This keeps the validator generic: it resolves the artifact path itself from the two
argv values and never needs to know the pipeline. A directory artifact counts as
non-empty when it contains at least one entry.

Pure and side-effect-free — safe to run from the walker, `cairn validate`, a retry
prompt, or CI.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: nonempty.py <run_dir> <artifact_name>")
        return 1

    run_dir, artifact_name = argv
    path = Path(run_dir) / artifact_name

    if not path.exists():
        print(f"artifact not found: {artifact_name}")
        return 1

    if path.is_dir():
        if not any(path.iterdir()):
            print(f"artifact directory is empty: {artifact_name}")
            return 1
        return 0

    if path.stat().st_size == 0:
        print(f"artifact is empty: {artifact_name}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
