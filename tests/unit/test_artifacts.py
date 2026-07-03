"""The artifact store: declaration parsing, path resolution, and the done-ness arbiter.

Behaviour tests against the public surface of ``cairn.kernel.artifacts`` — parsing the
pipeline's ``artifacts:`` mapping, resolving plain/glob paths inside a run dir, and the
schema+validator evaluation that decides whether a step is done (API §2.2, §4).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairn.kernel.artifacts import (
    ArtifactDecl,
    ResolvedArtifact,
    count_cycles,
    done,
    exists,
    parse_artifacts,
    resolve_path,
    validate,
)
from cairn.kernel.errors import ConfigError

FIXTURES = Path(__file__).parent / "fixtures"
VALIDATORS = FIXTURES / "validators"


def _decl(name="a", path="out.json", schema=None, validator=None):
    """A declaration wired straight to fixture files (bypasses parse for validate tests)."""
    return ArtifactDecl(
        name=name,
        path=path,
        schema=schema,
        validator=(VALIDATORS / validator) if validator else None,
    )


def _workspace(tmp_path: Path) -> Path:
    """A workspace with the schema/validator files the declarations reference."""
    (tmp_path / "schemas").mkdir()
    (tmp_path / "validators").mkdir()
    (tmp_path / "schemas" / "site-map.json").write_text("{}")
    (tmp_path / "validators" / "p0.py").write_text("#!/usr/bin/env python\n")
    return tmp_path


def test_parse_artifacts_builds_declarations_by_name(tmp_path):
    ws = _workspace(tmp_path)
    raw = {
        "site-map": {
            "path": "captures/site-map.json",
            "schema": "schemas/site-map.json",
            "validator": "validators/p0.py",
            "describe": "every page: url, type, sections[], images[]",
        }
    }
    decls = parse_artifacts(raw, ws)
    assert set(decls) == {"site-map"}
    decl = decls["site-map"]
    assert decl.name == "site-map"
    assert decl.path == "captures/site-map.json"
    assert decl.describe == "every page: url, type, sections[], images[]"
    # schema/validator are resolved to files under the workspace
    assert decl.schema == ws / "schemas" / "site-map.json"
    assert decl.validator == ws / "validators" / "p0.py"


# --------------------------------------------------------------------------- #
# Parsing errors
# --------------------------------------------------------------------------- #


def test_parse_rejects_declaration_with_neither_schema_nor_validator(tmp_path):
    raw = {"loose": {"path": "out.json"}}
    with pytest.raises(ConfigError, match="loose"):
        parse_artifacts(raw, tmp_path)


def test_parse_rejects_missing_schema_file(tmp_path):
    raw = {"site-map": {"path": "out.json", "schema": "schemas/nope.json"}}
    with pytest.raises(ConfigError) as exc:
        parse_artifacts(raw, tmp_path)
    assert "site-map" in str(exc.value)
    assert "schemas/nope.json" in str(exc.value)


def test_parse_rejects_missing_validator_file(tmp_path):
    raw = {"bp": {"path": "blueprints/**", "validator": "validators/gone.py"}}
    with pytest.raises(ConfigError) as exc:
        parse_artifacts(raw, tmp_path)
    assert "validators/gone.py" in str(exc.value)


def test_parse_rejects_missing_path(tmp_path):
    (tmp_path / "s.json").write_text("{}")
    raw = {"a": {"schema": "s.json"}}
    with pytest.raises(ConfigError, match="path"):
        parse_artifacts(raw, tmp_path)


# --------------------------------------------------------------------------- #
# Resolution & existence
# --------------------------------------------------------------------------- #


def test_resolve_plain_path_is_single_run_dir_child(tmp_path):
    resolved = resolve_path(_decl(path="captures/site-map.json"), "captures/site-map.json", tmp_path)
    assert resolved.paths == [tmp_path / "captures" / "site-map.json"]


def test_resolve_glob_returns_sorted_matches(tmp_path):
    (tmp_path / "blueprints").mkdir()
    (tmp_path / "blueprints" / "b.json").write_text("{}")
    (tmp_path / "blueprints" / "a.json").write_text("{}")
    resolved = resolve_path(_decl(path="blueprints/**"), "blueprints/*.json", tmp_path)
    assert [p.name for p in resolved.paths] == ["a.json", "b.json"]


def test_resolve_glob_with_no_matches_is_empty(tmp_path):
    resolved = resolve_path(_decl(path="blueprints/**"), "blueprints/*.json", tmp_path)
    assert resolved.paths == []
    assert exists(resolved) is False


def test_resolve_rejects_absolute_path(tmp_path):
    with pytest.raises(ConfigError, match="escapes"):
        resolve_path(_decl(), "/etc/passwd", tmp_path)


def test_resolve_rejects_parent_escape(tmp_path):
    with pytest.raises(ConfigError, match="escapes"):
        resolve_path(_decl(), "../../secrets.json", tmp_path)


def test_exists_true_only_when_plain_file_present(tmp_path):
    resolved = resolve_path(_decl(), "out.json", tmp_path)
    assert exists(resolved) is False
    (tmp_path / "out.json").write_text("{}")
    assert exists(resolve_path(_decl(), "out.json", tmp_path)) is True


# --------------------------------------------------------------------------- #
# Validation — schema
# --------------------------------------------------------------------------- #


def _schema(tmp_path, body):
    f = tmp_path / "schema.json"
    f.write_text(json.dumps(body))
    return f


def test_schema_pass(tmp_path):
    schema = _schema(tmp_path, {"type": "object", "required": ["url"]})
    (tmp_path / "out.json").write_text(json.dumps({"url": "x"}))
    decl = _decl(schema=schema)
    result = validate(resolve_path(decl, "out.json", tmp_path), decl, tmp_path, tmp_path)
    assert result.ok is True
    assert result.reasons == []


def test_schema_violation_becomes_reason(tmp_path):
    schema = _schema(tmp_path, {"type": "object", "required": ["url"]})
    (tmp_path / "out.json").write_text(json.dumps({"nope": 1}))
    decl = _decl(schema=schema)
    result = validate(resolve_path(decl, "out.json", tmp_path), decl, tmp_path, tmp_path)
    assert result.ok is False
    assert any("url" in r for r in result.reasons)


def test_bad_json_becomes_reason(tmp_path):
    schema = _schema(tmp_path, {"type": "object"})
    (tmp_path / "out.json").write_text("{not json")
    decl = _decl(schema=schema)
    result = validate(resolve_path(decl, "out.json", tmp_path), decl, tmp_path, tmp_path)
    assert result.ok is False
    assert any("invalid JSON" in r for r in result.reasons)


def test_schema_validates_every_matched_json_in_a_glob(tmp_path):
    schema = _schema(tmp_path, {"type": "object", "required": ["ok"]})
    (tmp_path / "bp").mkdir()
    (tmp_path / "bp" / "good.json").write_text(json.dumps({"ok": 1}))
    (tmp_path / "bp" / "bad.json").write_text(json.dumps({}))
    decl = ArtifactDecl(name="bp", path="bp/**", schema=schema)
    result = validate(resolve_path(decl, "bp/*.json", tmp_path), decl, tmp_path, tmp_path)
    assert result.ok is False
    assert any("bad.json" in r for r in result.reasons)
    assert not any("good.json" in r for r in result.reasons)


# --------------------------------------------------------------------------- #
# Validation — external validator
# --------------------------------------------------------------------------- #


def test_validator_exit0_passes(tmp_path):
    (tmp_path / "out.json").write_text("{}")
    decl = _decl(validator="pass.py")
    result = validate(resolve_path(decl, "out.json", tmp_path), decl, tmp_path, VALIDATORS)
    assert result.ok is True


def test_validator_exit1_reports_stdout_lines(tmp_path):
    (tmp_path / "out.json").write_text("{}")
    decl = _decl(validator="fail_reasons.py")
    result = validate(resolve_path(decl, "out.json", tmp_path), decl, tmp_path, VALIDATORS)
    assert result.ok is False
    assert result.reasons == [
        "section key 'heroX' not in catalog",
        "missing footer nav",
    ]


def test_validator_exit1_without_stdout_has_placeholder_reason(tmp_path):
    (tmp_path / "out.json").write_text("{}")
    decl = _decl(validator="fail_empty.py")
    result = validate(resolve_path(decl, "out.json", tmp_path), decl, tmp_path, VALIDATORS)
    assert result.reasons == ["validator failed without reasons"]


def test_validator_unexpected_exit_code_fails_naming_validator(tmp_path):
    (tmp_path / "out.json").write_text("{}")
    decl = _decl(validator="crash.py")
    result = validate(resolve_path(decl, "out.json", tmp_path), decl, tmp_path, VALIDATORS)
    assert result.ok is False
    assert any("crash.py" in r and "3" in r for r in result.reasons)


def test_validator_timeout_fails(tmp_path):
    (tmp_path / "out.json").write_text("{}")
    decl = _decl(validator="sleep.py")
    result = validate(
        resolve_path(decl, "out.json", tmp_path), decl, tmp_path, VALIDATORS, timeout_s=1
    )
    assert result.ok is False
    assert any("timed out" in r for r in result.reasons)


def test_executable_validator_runs_via_shebang(tmp_path):
    (tmp_path / "out.json").write_text("{}")
    decl = _decl(validator="pass.sh")
    result = validate(resolve_path(decl, "out.json", tmp_path), decl, tmp_path, VALIDATORS)
    assert result.ok is True


def test_validator_receives_rendered_artifact_path_as_argv3(tmp_path):
    # Contract: argv = [run_dir, artifact_name, artifact_path]. echo_argv3.py echoes argv[3].
    (tmp_path / "captures").mkdir()
    (tmp_path / "captures" / "site-map.json").write_text("{}")
    decl = _decl(name="site-map", path="captures/site-map.json", validator="echo_argv3.py")
    resolved = resolve_path(decl, "captures/site-map.json", tmp_path)
    result = validate(resolved, decl, tmp_path, VALIDATORS)
    assert result.reasons == ["argv3=captures/site-map.json"]


def test_glob_validator_receives_the_pattern_as_argv3(tmp_path):
    # Glob artifacts get the rendered *pattern* string, not a matched file.
    (tmp_path / "blueprints").mkdir()
    (tmp_path / "blueprints" / "a.json").write_text("{}")
    decl = ArtifactDecl(name="bp", path="blueprints/**", validator=VALIDATORS / "echo_argv3.py")
    resolved = resolve_path(decl, "blueprints/**", tmp_path)
    result = validate(resolved, decl, tmp_path, VALIDATORS)
    assert result.reasons == ["argv3=blueprints/**"]


# --------------------------------------------------------------------------- #
# Missing-artifact short-circuit
# --------------------------------------------------------------------------- #


def test_missing_artifact_short_circuits_before_validator(tmp_path):
    # sleep.py would hang for 30s if run; a missing file must skip it entirely.
    decl = _decl(validator="sleep.py")
    result = validate(
        resolve_path(decl, "out.json", tmp_path), decl, tmp_path, VALIDATORS, timeout_s=1
    )
    assert result.ok is False
    assert result.reasons == ["artifact missing: out.json"]


# --------------------------------------------------------------------------- #
# done() aggregate
# --------------------------------------------------------------------------- #


def test_done_true_when_all_produce_valid(tmp_path):
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "b.json").write_text("{}")
    da = _decl(name="a", path="a.json", validator="pass.py")
    db = _decl(name="b", path="b.json", validator="pass.py")
    decls = {"a": da, "b": db}
    resolved = [resolve_path(da, "a.json", tmp_path), resolve_path(db, "b.json", tmp_path)]
    ok, results = done(resolved, decls, tmp_path, VALIDATORS)
    assert ok is True
    assert set(results) == {"a", "b"}


def test_done_false_names_the_failing_artifact(tmp_path):
    (tmp_path / "a.json").write_text("{}")
    da = _decl(name="a", path="a.json", validator="pass.py")
    db = _decl(name="b", path="b.json", validator="pass.py")  # b.json missing
    decls = {"a": da, "b": db}
    resolved = [resolve_path(da, "a.json", tmp_path), resolve_path(db, "b.json", tmp_path)]
    ok, results = done(resolved, decls, tmp_path, VALIDATORS)
    assert ok is False
    assert results["a"].ok is True
    assert results["b"].ok is False


# --------------------------------------------------------------------------- #
# count_cycles
# --------------------------------------------------------------------------- #


def test_count_cycles_stops_at_first_gap(tmp_path):
    qa = tmp_path / "qa"
    qa.mkdir()
    for n in (1, 2, 4):  # cycle 3 is the gap
        (qa / f"art-review-r{n}.json").write_text("{}")
    decl = ArtifactDecl(name="art-review", path="qa/art-review-r{cycle}.json", schema=tmp_path)
    n = count_cycles(decl, tmp_path, lambda c: f"qa/art-review-r{c}.json")
    assert n == 2


def test_count_cycles_zero_when_none_exist(tmp_path):
    decl = ArtifactDecl(name="art-review", path="qa/art-review-r{cycle}.json", schema=tmp_path)
    assert count_cycles(decl, tmp_path, lambda c: f"qa/art-review-r{c}.json") == 0
