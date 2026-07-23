#!/usr/bin/env python3
"""poll-report-complete — fail when a poll-report is not complete.

Contract (docs/API.md §4): invoked as

    poll-report-complete.py <run_dir> <artifact_name> <artifact_path>
    exit 0 → pass; exit 1 + one reason per stdout line → fail

Cursor commit is gated on produces validation (docs/TRIGGERS.md). An incomplete
poll (rate-limit, pagination shortfall, backpressure pause) must not advance the
watermark: this validator fails on ``complete: false`` so the kernel withholds
the cursor commit. ``complete: true`` passes.

Shape checking beyond ``complete`` is the schema's job (schemas/poll-report.json);
this validator owns the completeness rule schemas cannot enforce as a side effect.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def check(doc: object) -> list[str]:
    """Failure reasons for a parsed poll-report — empty list = pass."""
    if not isinstance(doc, dict):
        return ["poll-report must be a JSON object"]
    if "complete" not in doc:
        return ["poll-report missing required field 'complete'"]
    complete = doc["complete"]
    if not isinstance(complete, bool):
        return [f"poll-report 'complete' must be a boolean, got {type(complete).__name__}"]
    if complete is False:
        return ["poll-report complete:false — cursor must not advance"]
    return []


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: poll-report-complete.py <run_dir> <artifact_name> <artifact_path>")
        return 1
    run_dir, _name, rel = Path(argv[0]), argv[1], argv[2]
    path = run_dir / rel
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"poll-report unreadable: {exc}")
        return 1
    except json.JSONDecodeError as exc:
        print(f"poll-report is not valid JSON: {exc}")
        return 1
    reasons = check(doc)
    for reason in reasons:
        print(reason)
    return 1 if reasons else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
