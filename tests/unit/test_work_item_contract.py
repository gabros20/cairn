"""W4-T2 — work-item contract: r<epoch> rev helper, schemas, validators.

Covers the pieces with testable logic (rev derivation + template furniture).
Puller scaffolds / write-back markers are W4-T3.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from cairn.kernel.queue_ledger import parse_item_name, rev_is_newer
from cairn.kernel.work_item import (
    REV_EPOCH_WIDTH,
    REV_VERSION_WIDTH,
    work_item_rev,
)

REPO = Path(__file__).parents[2]
TEMPLATE = REPO / "templates" / "workspace"
SCHEMAS = TEMPLATE / "schemas"
VALIDATORS = TEMPLATE / "validators"

# Fixed total digit count under the r marker (epoch + version).
_REV_DIGITS = REV_EPOCH_WIDTH + REV_VERSION_WIDTH


# --------------------------------------------------------------------------- #
# work_item_rev — fixed-width, monotonicity, bare↔versioned, parse round-trip
# --------------------------------------------------------------------------- #


def test_work_item_rev_form_and_fixed_width():
    token = work_item_rev("2024-01-15T12:00:00Z")
    assert token.startswith("r")
    assert token[1:].isdigit()
    assert len(token[1:]) == _REV_DIGITS
    # Known UTC epoch 1705320000, version default 0 — fixed pad both sides.
    assert token == f"r{1705320000:0{REV_EPOCH_WIDTH}d}{'0' * REV_VERSION_WIDTH}"


def test_work_item_rev_monotonic_later_timestamp_strictly_newer():
    older = work_item_rev("2024-01-15T12:00:00Z")
    newer = work_item_rev("2024-01-15T12:00:01Z")
    much_newer = work_item_rev("2025-06-01T00:00:00+00:00")
    assert rev_is_newer(newer[1:], older[1:]) is True
    assert rev_is_newer(older[1:], newer[1:]) is False
    assert rev_is_newer(much_newer[1:], newer[1:]) is True
    # Equal stamps → equal rev (not strictly newer).
    assert rev_is_newer(older[1:], work_item_rev("2024-01-15T12:00:00Z")[1:]) is False
    assert older == work_item_rev("2024-01-15T12:00:00+00:00")


def test_work_item_rev_version_tiebreak_within_one_second():
    base = "2024-01-15T12:00:00Z"
    v0 = work_item_rev(base, version=0)
    v1 = work_item_rev(base, version=1)
    v9 = work_item_rev(base, version=9)
    # Default version is 0 — same token as explicit 0.
    assert work_item_rev(base) == v0
    assert v0.endswith("0" * REV_VERSION_WIDTH)
    assert rev_is_newer(v1[1:], v0[1:]) is True
    assert rev_is_newer(v9[1:], v1[1:]) is True
    assert rev_is_newer(v0[1:], v1[1:]) is False
    assert len(v0[1:]) == len(v9[1:]) == _REV_DIGITS


def test_work_item_rev_bare_vs_versioned_across_epochs():
    """C1 repro: a 2024 versioned rev must NOT outrank a 2026 bare rev.

    Variable-width encoding made int("2024…version") > int("2026…") because the
    versioned form had more digits. Fixed width keeps chronology dominant.
    """
    earlier_versioned = work_item_rev("2024-01-01T00:00:00Z", version=5)
    later_bare = work_item_rev("2026-07-22T00:00:00Z")  # ~2.5y later, version=0
    assert len(earlier_versioned[1:]) == len(later_bare[1:]) == _REV_DIGITS
    assert rev_is_newer(later_bare[1:], earlier_versioned[1:]) is True
    assert rev_is_newer(earlier_versioned[1:], later_bare[1:]) is False
    # Same-epoch bare (v0) vs versioned: version wins.
    same = "2024-06-01T00:00:00Z"
    assert rev_is_newer(work_item_rev(same, version=1)[1:], work_item_rev(same)[1:]) is True


def test_work_item_rev_round_trip_parse_item_name():
    token = work_item_rev("2024-06-01T08:30:00Z", version=3)
    name = f"p3-github-issue42-{token}.json"
    item = parse_item_name(name)
    assert item is not None
    assert item.prio == 3
    assert item.source == "github"
    assert item.id == "issue42"
    assert item.rev == token[1:]
    assert item.filename == name
    assert rev_is_newer(item.rev, "0") is True


def test_work_item_rev_rejects_bad_inputs():
    with pytest.raises(ValueError):
        work_item_rev("")
    with pytest.raises(ValueError):
        work_item_rev("not-a-date")
    with pytest.raises(ValueError):
        work_item_rev("2024-01-15T12:00:00Z", version=-1)
    with pytest.raises(ValueError):
        work_item_rev("2024-01-15T12:00:00Z", version=10**REV_VERSION_WIDTH)


def test_work_item_rev_rejects_pre_epoch_fail_loud():
    """I1: pre-1970 → negative epoch must raise, not emit r-NN (grammar-breaking)."""
    with pytest.raises(ValueError, match="Unix epoch|pre-epoch|non-negative"):
        work_item_rev("1969-12-31T23:59:00Z")


# --------------------------------------------------------------------------- #
# Template schemas — valid JSON Schema + sample work-item validates
# --------------------------------------------------------------------------- #


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["work-item", "poll-report", "source-status"])
def test_template_schema_is_valid_json_schema(name: str):
    schema = _load_schema(name)
    # Draft 2020-12 meta-check: jsonschema can construct a validator.
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema.get("type") == "object"
    assert "properties" in schema


def _sample_work_item(*, prio: int = 3) -> dict:
    """Schema-conformant work-item body (canonical field names, incl. ``prio``)."""
    token = work_item_rev("2024-01-15T12:00:00Z")
    return {
        "id": "42",
        "source": "github",
        "title": "Fix the drain",
        "url": "https://example.com/issues/42",
        "prio": prio,
        "created": "2024-01-10T00:00:00Z",
        "updated_at": "2024-01-15T12:00:00Z",
        "rev": token[1:],
        "payload": {"labels": ["bug"]},
        "lane": "lit",
    }


def test_sample_work_item_validates_against_schema():
    schema = _load_schema("work-item")
    jsonschema.Draft202012Validator(schema).validate(_sample_work_item())
    # I2: prio 0 is legal (kernel grammar 0–9).
    jsonschema.Draft202012Validator(schema).validate(_sample_work_item(prio=0))


def test_schema_conformant_work_item_is_admitted_strict(tmp_path: Path):
    """Integration: schema-valid body + grammar filename → admit_strict ACCEPTS.

    Catches schema↔kernel drift on body-agreement fields (source, id, rev, prio).
    A field renamed only on one side (e.g. priority vs prio) fails this test.
    """
    from cairn.kernel.queue_ledger import admit_strict

    doc = _sample_work_item()
    schema = _load_schema("work-item")
    jsonschema.Draft202012Validator(schema).validate(doc)

    # Filename prio digit must match body["prio"]; rev digits match body["rev"].
    name = f"p{doc['prio']}-{doc['source']}-{doc['id']}-r{doc['rev']}.json"
    watch = tmp_path / "inbox"
    watch.mkdir()
    path = watch / name
    path.write_text(json.dumps(doc), encoding="utf-8")

    result = admit_strict(watch, path)
    assert result.disposition == "admit", (
        f"schema-conformant work-item rejected: {result.diagnostic!r} "
        f"(schema and kernel body-agreement must agree on field names)"
    )
    assert result.item is not None
    assert result.item.source == doc["source"]
    assert result.item.id == doc["id"]
    assert result.item.rev == doc["rev"]
    assert result.item.prio == doc["prio"]


def test_sample_poll_report_and_source_status_validate():
    pr = {
        "complete": True,
        "cursor": {"updated_at": "2024-01-15T12:00:00Z", "id": "42"},
        "emitted": 2,
        "source": "github",
    }
    jsonschema.Draft202012Validator(_load_schema("poll-report")).validate(pr)
    ss = {"status": "current", "checked_rev": "1705320000"}
    jsonschema.Draft202012Validator(_load_schema("source-status")).validate(ss)
    ss_changed = {
        "status": "changed",
        "checked_rev": "1705320000",
        "upstream_rev": "1705320100",
    }
    jsonschema.Draft202012Validator(_load_schema("source-status")).validate(ss_changed)


def test_source_status_schema_requires_upstream_rev_when_changed():
    schema = _load_schema("source-status")
    validator = jsonschema.Draft202012Validator(schema)
    missing = {"status": "changed", "checked_rev": "1"}
    errors = list(validator.iter_errors(missing))
    assert errors, "changed without upstream_rev must fail schema"
    assert any("upstream_rev" in e.message or "upstream_rev" in str(e.schema_path) for e in errors)


# --------------------------------------------------------------------------- #
# Validators — poll-report-complete + source-status pass/fail
# --------------------------------------------------------------------------- #


def _run_validator(script: str, run_dir: Path, name: str, rel: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(VALIDATORS / script), str(run_dir), name, rel],
        capture_output=True,
        text=True,
    )


def test_poll_report_complete_passes_on_complete_true(tmp_path: Path):
    (tmp_path / "poll-report.json").write_text(
        json.dumps(
            {
                "complete": True,
                "cursor": {"updated_at": "2024-01-15T12:00:00Z", "id": "1"},
                "emitted": 0,
                "source": "github",
            }
        ),
        encoding="utf-8",
    )
    r = _run_validator("poll-report-complete.py", tmp_path, "poll-report", "poll-report.json")
    assert r.returncode == 0, r.stdout + r.stderr


def test_poll_report_complete_fails_on_complete_false(tmp_path: Path):
    (tmp_path / "poll-report.json").write_text(
        json.dumps(
            {
                "complete": False,
                "cursor": {"updated_at": "2024-01-15T12:00:00Z", "id": "1"},
                "emitted": 0,
                "source": "github",
            }
        ),
        encoding="utf-8",
    )
    r = _run_validator("poll-report-complete.py", tmp_path, "poll-report", "poll-report.json")
    assert r.returncode == 1
    assert "complete:false" in r.stdout


def test_source_status_validator_passes_current(tmp_path: Path):
    (tmp_path / "source-status.json").write_text(
        json.dumps({"status": "current", "checked_rev": "1705320000"}),
        encoding="utf-8",
    )
    r = _run_validator("source-status.py", tmp_path, "source-status", "source-status.json")
    assert r.returncode == 0, r.stdout + r.stderr


def test_source_status_validator_fails_bad_status(tmp_path: Path):
    (tmp_path / "source-status.json").write_text(
        json.dumps({"status": "skippable", "checked_rev": "1"}),
        encoding="utf-8",
    )
    r = _run_validator("source-status.py", tmp_path, "source-status", "source-status.json")
    assert r.returncode == 1
    assert "status" in r.stdout


def test_source_status_validator_fails_missing_checked_rev(tmp_path: Path):
    (tmp_path / "source-status.json").write_text(
        json.dumps({"status": "closed"}),
        encoding="utf-8",
    )
    r = _run_validator("source-status.py", tmp_path, "source-status", "source-status.json")
    assert r.returncode == 1
    assert "checked_rev" in r.stdout


def test_source_status_validator_fails_changed_without_upstream_rev(tmp_path: Path):
    (tmp_path / "source-status.json").write_text(
        json.dumps({"status": "changed", "checked_rev": "1"}),
        encoding="utf-8",
    )
    r = _run_validator("source-status.py", tmp_path, "source-status", "source-status.json")
    assert r.returncode == 1
    assert "upstream_rev" in r.stdout
