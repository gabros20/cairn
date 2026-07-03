"""The workspace scaffold template — the tree `cairn new workspace` instantiates.

Asserts the file list from DISTRIBUTION §4 exists, that every YAML/TOML/JSON file
parses, that the starter validator honors its argv contract, and that the operator
skill carries all six operator rules.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

TEMPLATE = Path(__file__).parents[2] / "templates" / "workspace"

# The exact tree DISTRIBUTION §4 requires the scaffold to contain.
EXPECTED_FILES = [
    "cairn.toml",
    "pipelines/hello.yaml",
    "agents/assistant.yaml",
    "skills/cairn-operator/SKILL.md",
    "schemas/step-return.json",
    "schemas/greeting.json",
    "validators/nonempty.py",
    "prompts/DOCTRINE.md",
    "allowlist.yaml",
    ".gitignore",
    "README.md",
]

WORKSPACE_PLACEHOLDER = "{{WORKSPACE_NAME}}"


def test_every_scaffold_file_exists():
    missing = [rel for rel in EXPECTED_FILES if not (TEMPLATE / rel).is_file()]
    assert not missing, f"scaffold is missing: {missing}"


def test_yaml_files_parse():
    # hello.yaml + assistant.yaml carry content; allowlist.yaml ships as all-comment
    # empty fragments (valid YAML that safe_loads to None) — it must parse, not error.
    for rel in ("pipelines/hello.yaml", "agents/assistant.yaml"):
        doc = yaml.safe_load((TEMPLATE / rel).read_text(encoding="utf-8"))
        assert doc is not None, f"{rel} parsed to nothing"
    yaml.safe_load((TEMPLATE / "allowlist.yaml").read_text(encoding="utf-8"))


def test_json_files_parse():
    for rel in ("schemas/step-return.json", "schemas/greeting.json"):
        json.loads((TEMPLATE / rel).read_text(encoding="utf-8"))


def test_cairn_toml_parses_once_placeholder_substituted():
    text = (TEMPLATE / "cairn.toml").read_text(encoding="utf-8")
    assert WORKSPACE_PLACEHOLDER in text, "template must carry the {{WORKSPACE_NAME}} placeholder"
    rendered = text.replace(WORKSPACE_PLACEHOLDER, "test-workspace")
    data = tomllib.loads(rendered)
    assert data["workspace"]["name"] == "test-workspace"
    assert data["workspace"]["default_executor"] == "claude"


def test_cairn_toml_loads_through_the_kernel(tmp_path):
    # A rendered scaffold config must load cleanly through cairn's own loader.
    from cairn.kernel.config import load_config

    text = (TEMPLATE / "cairn.toml").read_text(encoding="utf-8")
    (tmp_path / "cairn.toml").write_text(
        text.replace(WORKSPACE_PLACEHOLDER, "test-workspace"), encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert cfg.workspace.name == "test-workspace"
    assert cfg.workspace.runs_dir == "runs"
    assert cfg.defaults.step_timeout_s == 1800  # "30m"


def test_hello_pipeline_shape():
    doc = yaml.safe_load((TEMPLATE / "pipelines/hello.yaml").read_text(encoding="utf-8"))
    assert doc["pipeline"] == "hello"
    assert doc["version"] == 1
    assert "name" in doc["params"]
    # >= 2 artifacts, one schema'd and one validator'd.
    artifacts = doc["artifacts"]
    assert len(artifacts) >= 2
    assert "schema" in artifacts["greeting"]
    assert artifacts["message"]["validator"] == "validators/nonempty.py"
    # The hard rule: the only executable step kinds day-0 are run: and gate.
    kinds = [("gate" if "gate" in n else "run" if "run" in n else "agent") for n in doc["steps"]]
    assert "gate" in kinds
    assert kinds.count("run") >= 2
    assert "agent" not in kinds, "no live agent: step — the day-0 pipeline must run auth-free"
    # The second run: step wires the gate choice + the first artifact together.
    compose = next(n for n in doc["steps"] if n.get("id") == "compose")
    assert "{gate:tone}" in compose["run"]
    assert "{artifact:greeting}" in compose["run"]
    assert set(compose["needs"]) == {"greeting", "tone"}


def test_hello_yaml_shows_the_commented_agent_upgrade():
    text = (TEMPLATE / "pipelines/hello.yaml").read_text(encoding="utf-8")
    assert "agent: assistant" in text, "hello.yaml must show the commented agent: upgrade"
    assert "agents/assistant.yaml" in text


def _run_nonempty(run_dir: Path, artifact_name: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TEMPLATE / "validators/nonempty.py"), str(run_dir), artifact_name],
        capture_output=True,
        text=True,
    )


def test_nonempty_validator_compiles():
    compile(
        (TEMPLATE / "validators/nonempty.py").read_text(encoding="utf-8"),
        "nonempty.py",
        "exec",
    )


def test_nonempty_passes_on_a_non_empty_file(tmp_path):
    (tmp_path / "message.txt").write_text("Friendly hello, world!\n", encoding="utf-8")
    result = _run_nonempty(tmp_path, "message.txt")
    assert result.returncode == 0, result.stdout + result.stderr


def test_nonempty_fails_on_missing_target(tmp_path):
    result = _run_nonempty(tmp_path, "message.txt")
    assert result.returncode == 1
    assert result.stdout.strip(), "a failing validator must print at least one reason line"


def test_nonempty_fails_on_empty_target(tmp_path):
    (tmp_path / "message.txt").write_text("", encoding="utf-8")
    result = _run_nonempty(tmp_path, "message.txt")
    assert result.returncode == 1
    assert result.stdout.strip()


def test_operator_skill_carries_all_six_rule_keywords():
    text = (TEMPLATE / "skills/cairn-operator/SKILL.md").read_text(encoding="utf-8").lower()
    for keyword in ("plan", "headless", "gate", "validator", "doctor", "never"):
        assert keyword in text, f"operator SKILL.md is missing the '{keyword}' rule"
