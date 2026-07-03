#!/usr/bin/env python3
"""Fleet sentinel validator: the summary must carry this workspace's UNIQUE sentinel token.

Contract (docs/API.md §4): invoked as ``fleet_sentinel.py <run_dir> <artifact_name> <artifact_path>``;
exit 0 = pass, exit 1 = fail with one machine-readable reason per stdout line.

Unlike the single-executor workspaces' `requires_token.py` (whose token is withheld to force a
deterministic halt), this token IS disclosed to the summarize step via its pipeline args — the
happy path is meant to pass. The value is unique to workspace-fleet, so a green gate proves the
final artifact was produced by THIS pipeline's last leg carrying the value end-to-end (and the
recorded stub replayed offline is byte-anchored to the same sentinel).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_FLEET_SENTINEL = "cairn-fleet-sentinel-e5a2c7"  # unique to workspace-fleet (grep-able)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: fleet_sentinel.py <run_dir> <artifact_name> <artifact_path>")
        return 1
    run_dir, _name, rel = argv
    path = Path(run_dir) / rel
    if not path.is_file():
        print(f"artifact not found: {rel}")
        return 1
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"artifact is not readable JSON: {exc}")
        return 1
    if not isinstance(doc, dict) or doc.get("token") != _FLEET_SENTINEL:
        print("required key 'token' is missing or is not the fleet sentinel from the step args")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
