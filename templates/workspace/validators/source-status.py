#!/usr/bin/env python3
"""source-status — schema + status enum for T6b refresh artifacts.

Contract (docs/API.md §4): invoked as

    source-status.py <run_dir> <artifact_name> <artifact_path>
    exit 0 → pass; exit 1 + one reason per stdout line → fail

Validates the artifact against schemas/source-status.json (status ∈
current|changed|closed, required checked_rev) so a refresh never emits a bare
skippable that would strand the walker (FACTORY-PLAN T6b).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import jsonschema
except ImportError:  # pragma: no cover — kernel dep; present in the cairn env
    jsonschema = None  # type: ignore[assignment]

_STATUS_VALUES = frozenset({"current", "changed", "closed"})


def _load_schema(run_dir: Path) -> dict | None:
    """Locate source-status.json next to this validator or under the workspace.

    Prefer the schema shipped beside this file (``../schemas/`` — the template
    layout). Fall back to ``<workspace>/schemas/`` derived from a typical
    ``runs/<id>`` run_dir so a copied validator still finds a workspace schema.
    """
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "schemas" / "source-status.json",
        run_dir.parent.parent / "schemas" / "source-status.json",
        run_dir / "schemas" / "source-status.json",
    ]
    for path in candidates:
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    return None


def check(doc: object, schema: dict | None = None) -> list[str]:
    """Failure reasons for a parsed source-status document — empty list = pass."""
    reasons: list[str] = []
    if not isinstance(doc, dict):
        return ["source-status must be a JSON object"]
    status = doc.get("status")
    if status not in _STATUS_VALUES:
        reasons.append(
            f"source-status 'status' must be one of "
            f"{sorted(_STATUS_VALUES)}, got {status!r}"
        )
    checked = doc.get("checked_rev")
    if not isinstance(checked, str) or not checked:
        reasons.append("source-status requires non-empty string 'checked_rev'")
    if status == "changed":
        upstream = doc.get("upstream_rev")
        if not isinstance(upstream, str) or not upstream:
            reasons.append(
                "source-status status=changed requires non-empty string 'upstream_rev'"
            )
    if schema is not None and jsonschema is not None:
        validator = jsonschema.Draft202012Validator(schema)
        for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.path)):
            path = ".".join(str(p) for p in err.path) or "(root)"
            reasons.append(f"schema: {path}: {err.message}")
    # Dedupe while preserving order (enum check + schema may both fire).
    seen: set[str] = set()
    out: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: source-status.py <run_dir> <artifact_name> <artifact_path>")
        return 1
    run_dir, _name, rel = Path(argv[0]), argv[1], argv[2]
    path = run_dir / rel
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"source-status unreadable: {exc}")
        return 1
    except json.JSONDecodeError as exc:
        print(f"source-status is not valid JSON: {exc}")
        return 1
    schema = _load_schema(run_dir)
    reasons = check(doc, schema=schema)
    for reason in reasons:
        print(reason)
    return 1 if reasons else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
