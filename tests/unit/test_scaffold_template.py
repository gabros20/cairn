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
    # The self-improve furniture (TOOLING-AND-GROWTH §7 — the learning loop's
    # curate→promote stages as packaged mechanism; policy stays workspace-owned).
    "pipelines/self-improve.yaml",
    "agents/curator.yaml",
    "skills/self-improve-curator/SKILL.md",
    "schemas/self-improve-proposals.json",
    "validators/self-improve-proposals.py",
    "scripts/self-improve-open-pr.py",
    "tests/fixtures/proposals/valid-two-promotions.json",
    "tests/fixtures/proposals/invalid-target-escapes.json",
    "tests/fixtures/proposals/invalid-missing-rationale.json",
    "tests/stubs/self-improve/curate/proposals.json",
    "tests/matrix.yaml",
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


def test_step_return_schema_byte_equals_kernel_resource():
    # Drift guard: the scaffold ships a copy of the kernel's step-return schema. They
    # are equal today; this fails the moment they diverge byte-for-byte (raw bytes, not
    # parsed JSON — so whitespace/key-order drift is caught too). Locally perturbing one
    # byte in either file makes this go red; restoring it makes it green again.
    from importlib import resources

    packaged = resources.files("cairn.resources.schemas").joinpath("step-return.schema.json").read_bytes()
    scaffold = (TEMPLATE / "schemas/step-return.json").read_bytes()
    assert scaffold == packaged, (
        "templates/workspace/schemas/step-return.json has drifted from the packaged "
        "cairn.resources.schemas/step-return.schema.json — re-copy the kernel resource"
    )


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
    compose = next(n for n in doc["steps"] if n.get("step", n.get("id")) == "compose")
    assert "{gate:tone}" in compose["run"]
    assert "{artifact:greeting}" in compose["run"]
    assert set(compose["needs"]) == {"greeting", "tone"}


def test_hello_yaml_shows_the_commented_agent_upgrade():
    text = (TEMPLATE / "pipelines/hello.yaml").read_text(encoding="utf-8")
    assert "agent: assistant" in text, "hello.yaml must show the commented agent: upgrade"
    assert "agents/assistant.yaml" in text


def _run_nonempty(run_dir: Path, *args: str) -> subprocess.CompletedProcess:
    # Validator contract argv = [run_dir, artifact_name, artifact_path] (docs/API.md §4);
    # *args lets a test pass the 3-arg form or the 2-arg legacy fallback.
    return subprocess.run(
        [sys.executable, str(TEMPLATE / "validators/nonempty.py"), str(run_dir), *args],
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
    # 3-arg form: name + rendered path (both message.txt here).
    result = _run_nonempty(tmp_path, "message", "message.txt")
    assert result.returncode == 0, result.stdout + result.stderr


def test_nonempty_fails_on_missing_target(tmp_path):
    result = _run_nonempty(tmp_path, "message", "message.txt")
    assert result.returncode == 1
    assert result.stdout.strip(), "a failing validator must print at least one reason line"


def test_nonempty_fails_on_empty_target(tmp_path):
    (tmp_path / "message.txt").write_text("", encoding="utf-8")
    result = _run_nonempty(tmp_path, "message", "message.txt")
    assert result.returncode == 1
    assert result.stdout.strip()


def test_nonempty_uses_path_argv_not_name(tmp_path):
    # The rendered path (argv[3]), not the logical name (argv[2]), locates the file.
    (tmp_path / "greeting.json").write_text("{}", encoding="utf-8")
    ok = _run_nonempty(tmp_path, "greeting", "greeting.json")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    # Same file, but a wrong path → fail, proving the name is not what's resolved.
    miss = _run_nonempty(tmp_path, "greeting", "nope.json")
    assert miss.returncode == 1


def test_nonempty_two_arg_fallback(tmp_path):
    # Legacy callers pass only [run_dir, artifact_name]; the name is used as the path.
    (tmp_path / "message.txt").write_text("hi\n", encoding="utf-8")
    assert _run_nonempty(tmp_path, "message.txt").returncode == 0
    assert _run_nonempty(tmp_path, "missing.txt").returncode == 1


def test_nonempty_glob_artifact(tmp_path):
    # Glob artifact (e.g. blueprints/**): pass iff ≥1 match and every matched file non-empty.
    blueprints = tmp_path / "blueprints"
    blueprints.mkdir()
    assert _run_nonempty(tmp_path, "blueprints", "blueprints/**").returncode == 1  # no matches yet
    (blueprints / "home.json").write_text("{}", encoding="utf-8")
    assert _run_nonempty(tmp_path, "blueprints", "blueprints/**").returncode == 0
    (blueprints / "about.json").write_text("", encoding="utf-8")  # an empty match
    assert _run_nonempty(tmp_path, "blueprints", "blueprints/**").returncode == 1


def test_operator_skill_carries_all_six_rule_keywords():
    text = (TEMPLATE / "skills/cairn-operator/SKILL.md").read_text(encoding="utf-8").lower()
    for keyword in ("plan", "headless", "gate", "validator", "doctor", "never"):
        assert keyword in text, f"operator SKILL.md is missing the '{keyword}' rule"
