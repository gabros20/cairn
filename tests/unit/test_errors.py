"""Kernel error hierarchy: ConfigError carries findings + file/line, ValidationFailure carries reasons."""

from __future__ import annotations

import pytest

from cairn.kernel.errors import CairnError, ConfigError, ValidationFailure
from cairn.kernel.types import Finding


def test_config_error_is_a_cairn_error():
    assert issubclass(ConfigError, CairnError)
    assert issubclass(ValidationFailure, CairnError)


def test_config_error_carries_findings_and_optional_location():
    findings = [Finding(level="error", message="bad tier 'genius'")]
    err = ConfigError("invalid config", findings=findings, file="cairn.toml", line=42)
    assert err.findings == findings
    assert err.file == "cairn.toml"
    assert err.line == 42
    # message survives str()
    assert "invalid config" in str(err)


def test_config_error_findings_default_empty():
    err = ConfigError("nope")
    assert err.findings == []
    assert err.file is None
    assert err.line is None


def test_validation_failure_carries_reasons():
    err = ValidationFailure("artifact invalid", reasons=["missing key 'sections'", "empty images[]"])
    assert err.reasons == ["missing key 'sections'", "empty images[]"]
    with pytest.raises(CairnError):
        raise err
