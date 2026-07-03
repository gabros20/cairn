#!/usr/bin/env python3
"""Failure-path smoke validator: the report must carry a secret `token` the prompt withholds.

Contract (docs/API.md §4): invoked as ``requires_token.py <run_dir> <artifact_name> <artifact_path>``;
exit 0 = pass, exit 1 = fail with one machine-readable reason per stdout line (those reasons flow
into the trail, the halt message, and the retry envelope).

This validator can NEVER be satisfied by the agent: it requires ``token`` to equal a fixed value
that the smokefail prompt never reveals, and the failure reason deliberately does not leak it. So a
real live run halts after exhausting its retries — exercising retry-with-feedback then a GATE_FAILED
halt (exit 3) on the real claude executor, deterministically.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REQUIRED_TOKEN = "cairn-live-secret-2f9a"  # intentionally never disclosed to the agent


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: requires_token.py <run_dir> <artifact_name> <artifact_path>")
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
    if not isinstance(doc, dict) or doc.get("token") != _REQUIRED_TOKEN:
        # Reason names the missing key but never its required value — unsatisfiable by design.
        print("required key 'token' is missing or does not match the required value")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
