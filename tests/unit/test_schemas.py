"""Bundled JSON Schemas load, are themselves valid JSON Schema, and gate STEP/run shapes."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator, ValidationError

from cairn.kernel.schemas import get_schema


def test_step_return_schema_is_valid_json_schema():
    Draft202012Validator.check_schema(get_schema("step-return"))


def test_run_schema_is_valid_json_schema():
    Draft202012Validator.check_schema(get_schema("run"))


def test_step_return_accepts_a_valid_step_block():
    validator = Draft202012Validator(get_schema("step-return"))
    validator.validate(
        {
            "status": "done",
            "summary": "captured 19 pages",
            "artifacts": ["captures/site-map.json"],
            "metrics": {"pages": 19},
            "learnings": [{"note": "site never idles", "tag": "capture"}],
        }
    )


def test_step_return_rejects_bogus_status():
    validator = Draft202012Validator(get_schema("step-return"))
    with pytest.raises(ValidationError):
        validator.validate({"status": "bogus", "summary": "x", "artifacts": []})


def test_step_return_requires_core_fields():
    validator = Draft202012Validator(get_schema("step-return"))
    with pytest.raises(ValidationError):
        validator.validate({"summary": "no status or artifacts"})


def test_run_schema_requires_pinned_fields():
    validator = Draft202012Validator(get_schema("run"))
    with pytest.raises(ValidationError):
        validator.validate({"run_id": "acme-redesign-20260703"})  # missing the rest


def test_get_schema_unknown_name_raises():
    with pytest.raises(FileNotFoundError):
        get_schema("does-not-exist")
