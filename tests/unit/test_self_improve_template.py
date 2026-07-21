"""The self-improve template — the learning loop's curate→promote stages as packaged
workspace furniture (docs/TOOLING-AND-GROWTH.md §7).

The framework ships the MECHANISM (`pipelines/self-improve.yaml` + curator agent +
proposals contract + open-pr script, scaffolded by `cairn new workspace`); the workspace
owns the POLICY. These tests hold the mechanism to its hard rules:

* the scaffolded pipeline PLANS green with zero subprocesses;
* the proposals artifact contract accepts the valid fixture and rejects broken ones
  with reasons (schema layer + validator layer);
* the approve gate's headless default is "no" — an unattended (cron/batch) run records
  the proposals and stops; it can never self-promote;
* the open-pr step applies approved edits on a NEW branch and opens the PR via `gh`,
  never committing to the working branch (offline: stub curate, shimmed git push, fake
  gh — nothing touches a network or a real remote);
* `cairn new pipeline self-improve` retrofits the furniture into an existing workspace.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from cairn.cli import main
from cairn.executors.shell import ShellExecutor
from cairn.executors.stub import StubExecutor
from cairn.kernel import newkit, testkit
from cairn.kernel.compose import make_composer
from cairn.kernel.config import load_config
from cairn.kernel.plan import plan as build_plan
from cairn.kernel.runstate import load_run, node_status
from cairn.kernel.types import ExitCode
from cairn.kernel.walk import bootstrap_run, walk

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "templates" / "workspace"
NOW = datetime(2026, 7, 3, 12, 0)

PIPELINE = "pipelines/self-improve.yaml"
VALIDATOR = "validators/self-improve-proposals.py"
SCRIPT = "scripts/self-improve-open-pr.py"
STUB_PROPOSALS = "tests/stubs/self-improve/curate/proposals.json"


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """A fresh workspace instantiated from the packaged scaffold."""
    return newkit.new_workspace("improver", tmp_path)


# --------------------------------------------------------------------------- #
# Shims — every binary a walk may spawn, so no test touches a network or a
# real remote. PATH-prepended (the walker passes PATH through to run: steps).
# --------------------------------------------------------------------------- #


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _shim_bin(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A bin dir with `cairn` (real, via this venv), `gh` (recorder), `git` (recorder that
    delegates to the real git but intercepts `push`). Returns (bindir, gh_log, git_log)."""
    bindir = tmp_path / "shim-bin"
    bindir.mkdir(exist_ok=True)
    gh_log = tmp_path / "gh.log"
    git_log = tmp_path / "git.log"
    _write_exec(bindir / "cairn", f'#!/bin/sh\nexec "{sys.executable}" -m cairn "$@"\n')
    _write_exec(
        bindir / "gh",
        f'#!/bin/sh\necho "gh $*" >> "{gh_log}"\n'
        'case "$*" in *"pr create"*|"pr create"*) echo "https://github.com/example/repo/pull/7";; esac\n'
        "exit 0\n",
    )
    real_git = shutil.which("git")
    _write_exec(
        bindir / "git",
        f'#!/bin/sh\necho "git $*" >> "{git_log}"\n'
        'for a in "$@"; do\n'
        '  case "$a" in push) echo "push intercepted (offline test)"; exit 0;; esac\n'
        "done\n"
        f'exec "{real_git}" "$@"\n',
    )
    return bindir, gh_log, git_log


def _with_shims(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    bindir, gh_log, git_log = _shim_bin(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return gh_log, git_log


def _walk_self_improve(ws: Path, *, presets: dict | None = None):
    """Plan + bootstrap + walk self-improve headlessly; curate replays the shipped stub."""
    config = load_config(ws)
    p = build_plan(ws, "self-improve", {}, now=NOW, headless=True)
    run_dir = bootstrap_run(ws, p, now=NOW, runs_root=ws / "runs")
    stub = StubExecutor()  # resolves <ws>/tests/stubs via CAIRN_WORKSPACE at invoke time
    executors = {"shell": ShellExecutor(), "stub": stub}
    for exec_name, _model, _effort in p.resolved_models.values():
        executors[exec_name] = stub
    code = walk(
        p, run_dir,
        workspace_dir=ws, config=config, executors=executors,
        composer=make_composer(workspace_dir=ws, config=config, now=NOW),
        interactive=False, gate_presets=presets or {}, now=NOW,
    )
    return code, run_dir


# --------------------------------------------------------------------------- #
# 1. The scaffolded pipeline plans green — statically, zero subprocesses.
# --------------------------------------------------------------------------- #


def test_plan_green_with_zero_subprocesses(ws: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(ws)

    def _no_subprocess(*a, **k):  # pragma: no cover - fires only on regression
        raise AssertionError("cairn plan must never spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", _no_subprocess)
    assert main(["plan", "self-improve"]) == int(ExitCode.OK)
    out = capsys.readouterr().out
    assert "self-improve" in out


def test_pipeline_shape_matches_the_docs_sketch(ws: Path) -> None:
    doc = yaml.safe_load((ws / PIPELINE).read_text(encoding="utf-8"))
    assert doc["pipeline"] == "self-improve"
    # aggregate: a run: step over `cairn learnings`, honoring the since/tag params.
    agg = next(n for n in doc["steps"] if n.get("step", n.get("id")) == "aggregate")
    assert "cairn learnings" in agg["run"]
    assert "{params.since}" in agg["run"] and "{params.tag}" in agg["run"]
    assert "{artifact:learnings-snapshot}" in agg["run"]
    # curate: an agent step producing the typed proposals artifact.
    curate = next(n for n in doc["steps"] if n.get("step", n.get("id")) == "curate")
    assert curate["agent"] == "curator"
    assert curate["produces"] == ["proposals"]
    arts = doc["artifacts"]
    assert arts["proposals"]["schema"] == "schemas/self-improve-proposals.json"
    assert arts["proposals"]["validator"] == VALIDATOR
    # approve: a human gate whose headless default is the string "no" (quoted in YAML —
    # an unquoted `no` parses to False and would silently break the headless contract).
    gate = next(n for n in doc["steps"] if "gate" in n)
    assert gate["gate"] == "approve"
    assert gate["default"] == "no"
    assert set(gate["options"]) == {"yes", "no"}
    # open-pr: runs ONLY on an explicit yes.
    opr = next(n for n in doc["steps"] if n.get("step", n.get("id")) == "open-pr")
    assert opr["when"] == "gates.approve.choice == 'yes'"
    assert SCRIPT in opr["run"]


def test_hard_rule_is_baked_into_the_files_not_just_docs(ws: Path) -> None:
    # "Suggestions, not truth" must survive workspace customization: the rule lives in
    # the pipeline header, the script header, and the curator doctrine.
    for rel in (PIPELINE, SCRIPT, "skills/self-improve-curator/SKILL.md"):
        text = (ws / rel).read_text(encoding="utf-8").lower()
        assert "suggestions, not" in text, f"{rel} lost the suggestions-not-truth rule"
    assert "never" in (ws / SCRIPT).read_text(encoding="utf-8").lower()


def test_gh_is_declared_and_scoped_to_open_pr(ws: Path) -> None:
    # Dogfoods tool enforcement: plan flags the scoped requirement; run hard-stops when
    # the check fails — for THIS pipeline only.
    data = tomllib.loads((ws / "cairn.toml").read_text(encoding="utf-8"))
    gh = data["tools"]["gh"]
    assert gh["needed_by"] == ["open-pr"]
    assert "gh" in gh["check"]


def test_curator_agent_is_executor_agnostic_and_tier_routed(ws: Path) -> None:
    text = (ws / "agents/curator.yaml").read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert doc["tier"] in ("reasoning", "balanced", "cheap")
    assert "self-improve-curator" in doc["skills"]
    corpus = (text + (ws / "skills/self-improve-curator/SKILL.md").read_text(encoding="utf-8")).lower()
    for vendor in ("claude", "codex", "grok", "gpt", "opus", "sonnet", "haiku", "anthropic", "openai"):
        assert vendor not in corpus, f"curator furniture must not assume a vendor: {vendor!r}"


# --------------------------------------------------------------------------- #
# 2. The proposals contract — schema + validator + shipped fixtures.
# --------------------------------------------------------------------------- #


def _run_proposals_validator(ws: Path, run_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ws / VALIDATOR), str(run_dir), "proposals", "proposals.json"],
        capture_output=True,
        text=True,
    )


def test_validator_accepts_the_valid_fixture(ws: Path, tmp_path: Path) -> None:
    fixture = ws / "tests/fixtures/proposals/valid-two-promotions.json"
    run_dir = tmp_path / "rd"
    run_dir.mkdir()
    shutil.copy(fixture, run_dir / "proposals.json")
    res = _run_proposals_validator(ws, run_dir)
    assert res.returncode == 0, res.stdout + res.stderr


def test_validator_rejects_an_escaping_target_with_a_reason(ws: Path, tmp_path: Path) -> None:
    fixture = ws / "tests/fixtures/proposals/invalid-target-escapes.json"
    run_dir = tmp_path / "rd"
    run_dir.mkdir()
    shutil.copy(fixture, run_dir / "proposals.json")
    res = _run_proposals_validator(ws, run_dir)
    assert res.returncode == 1
    assert res.stdout.strip(), "a failing validator must print at least one reason line"


def test_validator_rejects_unparsable_json(ws: Path, tmp_path: Path) -> None:
    run_dir = tmp_path / "rd"
    run_dir.mkdir()
    (run_dir / "proposals.json").write_text("{not json", encoding="utf-8")
    res = _run_proposals_validator(ws, run_dir)
    assert res.returncode == 1
    assert res.stdout.strip()


def test_shipped_fixtures_pass_the_workspace_validator_suite(ws: Path) -> None:
    # The full contract (JSON Schema + validator script) through cairn's own testkit:
    # valid-* fixtures pass, invalid-* are rejected WITH reasons. This is exactly what
    # `cairn test` runs inside a scaffolded workspace.
    result = testkit.run_validator_suite(ws)
    assert result.failed == 0, result.failures
    assert result.passed >= 3  # 1 valid + 2 invalid fixtures ship with the scaffold


# --------------------------------------------------------------------------- #
# 3. The walk — offline, stubbed curate, shimmed git/gh.
# --------------------------------------------------------------------------- #


def test_headless_walk_gate_defaults_no_and_never_promotes(ws: Path, tmp_path: Path, monkeypatch) -> None:
    gh_log, _git_log = _with_shims(monkeypatch, tmp_path)

    code, run_dir = _walk_self_improve(ws)

    assert code == ExitCode.OK
    # aggregate ran the real `cairn learnings` verb; curate replayed the shipped stub.
    assert (run_dir / "learnings-snapshot.txt").stat().st_size > 0
    proposals = json.loads((run_dir / "proposals.json").read_text(encoding="utf-8"))
    assert proposals["proposals"], "the stub curate output must carry proposals"
    # The gate resolved to its DEFAULT — "no" — because nobody was there to say yes.
    gate = json.loads((run_dir / "gates/approve.json").read_text(encoding="utf-8"))
    assert gate["choice"] == "no"
    assert gate["by"] == "default"
    # …so open-pr was skipped: no branch, no PR, no gh — a cron run cannot self-promote.
    assert node_status(load_run(run_dir), "open-pr") == "skipped"
    assert not gh_log.exists(), "gh must never run when the gate says no"
    assert not (run_dir / "pr.json").exists()


def test_preset_yes_walk_applies_edits_on_a_new_branch_and_opens_pr(ws: Path, tmp_path: Path, monkeypatch) -> None:
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git required")
    gh_log, git_log = _with_shims(monkeypatch, tmp_path)

    # The workspace is a real git repo — but push is shim-intercepted and gh is fake,
    # so nothing can reach a remote.
    real_git = shutil.which("git")

    def git(*args: str) -> str:
        res = subprocess.run([real_git, "-C", str(ws), *args], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        return res.stdout.strip()

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("add", "-A")
    git("commit", "-m", "scaffold")
    working_branch = git("rev-parse", "--abbrev-ref", "HEAD")
    head_before = git("rev-parse", "HEAD")

    code, run_dir = _walk_self_improve(ws, presets={"approve": "yes"})

    assert code == ExitCode.OK
    record = json.loads((run_dir / "pr.json").read_text(encoding="utf-8"))
    branch = record["branch"]
    assert branch.startswith("self-improve/")
    assert branch != working_branch
    assert record["pr_url"] == "https://github.com/example/repo/pull/7"
    assert record["applied"] and not record.get("skipped")

    # The hard rule, observed: the working branch and tree are untouched…
    assert git("rev-parse", "--abbrev-ref", "HEAD") == working_branch
    assert git("rev-parse", "HEAD") == head_before
    assert git("status", "--porcelain") == ""
    # …and the new branch holds exactly the approved proposals' edits.
    git("rev-parse", "--verify", branch)
    touched = set(git("diff", "--name-only", f"{working_branch}..{branch}").splitlines())
    stub = json.loads((ws / STUB_PROPOSALS).read_text(encoding="utf-8"))
    assert touched == {p["target"] for p in stub["proposals"]}
    # The PR went through the (fake) gh; the push never left the machine.
    assert "pr create" in gh_log.read_text(encoding="utf-8")
    assert "push" in git_log.read_text(encoding="utf-8")


def test_matrix_row_walks_self_improve_through_cairn_test(ws: Path, tmp_path: Path, monkeypatch) -> None:
    # The scaffold ships tests/matrix.yaml + the curate stub, so a fresh workspace's own
    # `cairn test` exercises the loop headlessly (gate → "no", open-pr skipped).
    _with_shims(monkeypatch, tmp_path)
    result = testkit.run_pipeline_suite(ws)
    assert result.failed == 0, result.failures
    assert result.passed >= 2  # hello + self-improve rows


# --------------------------------------------------------------------------- #
# 4. The open-pr script's own target guard — defense in depth.
#
# The validator guarantees safe targets on the NORMAL path, but the documented
# per-proposal veto (a human editing proposals.json at the gate) happens AFTER
# validation — so the script must independently refuse a target that escapes the
# worktree; a refused target is just a failed-to-apply proposal, never a write.
# --------------------------------------------------------------------------- #


def _git_workspace(ws: Path) -> None:
    real_git = shutil.which("git")
    for args in (
        ("init",), ("config", "user.email", "t@example.com"), ("config", "user.name", "T"),
        ("add", "-A"), ("commit", "-m", "scaffold"),
    ):
        res = subprocess.run([real_git, "-C", str(ws), *args], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr


def _run_open_pr(
    ws: Path, tmp_path: Path, monkeypatch, proposals: list[dict], *, shims_only: tuple[str, ...] = ()
) -> tuple[subprocess.CompletedProcess, Path, Path]:
    """Invoke the script directly, the way the open-pr step does — shimmed PATH, a
    CONTROLLED TMPDIR (so temp leftovers are observable), no network. With
    ``shims_only``, PATH is REPLACED by the shim dir minus the named binaries — so a
    'missing tool' scenario can never fall through to the real machine's binary."""
    _with_shims(monkeypatch, tmp_path)
    run_dir = tmp_path / "runs" / "self-improve-20260703"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "proposals.json").write_text(json.dumps({"proposals": proposals}), encoding="utf-8")
    tmpdir = tmp_path / "controlled-tmp"
    tmpdir.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({"CAIRN_WORKSPACE": str(ws), "CAIRN_RUN_DIR": str(run_dir), "TMPDIR": str(tmpdir)})
    if shims_only:
        for name in shims_only:
            (tmp_path / "shim-bin" / name).unlink()
        env["PATH"] = str(tmp_path / "shim-bin")
    res = subprocess.run(
        [sys.executable, str(ws / SCRIPT), str(run_dir / "proposals.json"), str(run_dir / "pr.json")],
        capture_output=True, text=True, env=env,
    )
    return res, run_dir, tmpdir


def _malicious(tmp_path: Path) -> list[dict]:
    return [
        {"id": "abs", "promotion": "prompt", "target": str(tmp_path / "evil-absolute.md"),
         "action": "create", "text": "outside", "rationale": "injected at the gate"},
        {"id": "dotdot", "promotion": "prompt", "target": "../evil-relative.md",
         "action": "create", "text": "outside", "rationale": "injected at the gate"},
    ]


def test_open_pr_refuses_targets_injected_after_validation(ws: Path, tmp_path: Path, monkeypatch) -> None:
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git required")
    _git_workspace(ws)

    res, _run_dir, tmpdir = _run_open_pr(ws, tmp_path, monkeypatch, _malicious(tmp_path))

    # Every proposal was hostile → nothing applied → the script refuses (exit 1)…
    assert res.returncode == 1, res.stdout + res.stderr
    # …and, crucially, not a byte landed outside the worktree.
    assert not (tmp_path / "evil-absolute.md").exists()
    assert not (tmp_path / "evil-relative.md").exists()
    assert not (ws.parent / "evil-relative.md").exists()
    # All-fail cleanup: the now-pointless branch ref and the temp dir are both gone.
    branches = subprocess.run(
        [shutil.which("git"), "-C", str(ws), "branch", "--list", "self-improve/*"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert branches == "", f"stale branch left behind: {branches}"
    assert list(tmpdir.iterdir()) == [], "mkdtemp parent dir was not cleaned up"


def test_open_pr_skips_hostile_targets_but_still_applies_the_good_ones(ws: Path, tmp_path: Path, monkeypatch) -> None:
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git required")
    _git_workspace(ws)
    good = {"id": "good", "promotion": "prompt", "target": "prompts/DOCTRINE.md",
            "action": "append", "text": "\n## A fine rule\n", "rationale": "recurred"}

    res, run_dir, tmpdir = _run_open_pr(ws, tmp_path, monkeypatch, [*_malicious(tmp_path), good])

    # The run is otherwise unchanged: the good edit ships, the hostile ones are
    # reported as failed-to-apply — never written.
    assert res.returncode == 0, res.stdout + res.stderr
    record = json.loads((run_dir / "pr.json").read_text(encoding="utf-8"))
    assert record["applied"] == ["prompts/DOCTRINE.md"]
    assert {s["id"] for s in record["skipped"]} == {"abs", "dotdot"}
    assert not (tmp_path / "evil-absolute.md").exists()
    assert not (tmp_path / "evil-relative.md").exists()
    assert list(tmpdir.iterdir()) == [], "mkdtemp parent dir was not cleaned up"


def test_open_pr_skips_proposals_missing_required_fields(ws: Path, tmp_path: Path, monkeypatch) -> None:
    # The gate-time veto path again: a human may delete keys, not just whole
    # proposals — a missing field is a skip-with-reason, never a traceback.
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git required")
    _git_workspace(ws)
    no_action = {"id": "no-action", "promotion": "prompt", "target": "prompts/DOCTRINE.md",
                 "text": "x", "rationale": "r"}
    no_rationale = {"id": "no-rationale", "promotion": "prompt", "target": "prompts/DOCTRINE.md",
                    "action": "append", "text": "x"}
    good = {"id": "good", "promotion": "prompt", "target": "prompts/DOCTRINE.md",
            "action": "append", "text": "\n## A fine rule\n", "rationale": "recurred"}

    res, run_dir, _tmpdir = _run_open_pr(ws, tmp_path, monkeypatch, [no_action, no_rationale, good])

    assert res.returncode == 0, res.stdout + res.stderr
    assert "Traceback" not in res.stderr
    record = json.loads((run_dir / "pr.json").read_text(encoding="utf-8"))
    assert record["applied"] == ["prompts/DOCTRINE.md"]
    assert {s["id"] for s in record["skipped"]} == {"no-action", "no-rationale"}
    assert all("missing required field" in s["reason"] for s in record["skipped"])


def test_open_pr_all_missing_fields_fails_clean_without_traceback(ws: Path, tmp_path: Path, monkeypatch) -> None:
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git required")
    _git_workspace(ws)
    broken = {"id": "no-text", "promotion": "prompt", "target": "prompts/DOCTRINE.md",
              "action": "append", "rationale": "r"}

    res, _run_dir, tmpdir = _run_open_pr(ws, tmp_path, monkeypatch, [broken])

    assert res.returncode == 1
    assert "Traceback" not in res.stderr
    branches = subprocess.run(
        [shutil.which("git"), "-C", str(ws), "branch", "--list", "self-improve/*"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert branches == "", f"stale branch left behind: {branches}"
    assert list(tmpdir.iterdir()) == []


def test_open_pr_missing_gh_is_legible_and_keeps_the_committed_branch(ws: Path, tmp_path: Path, monkeypatch) -> None:
    # `gh` vanishing between the [tools] preflight and the PR call must not traceback:
    # the failure names the missing binary AND the surviving branch (the commit is
    # valuable — a retry pushes it; only the all-fail path deletes the branch).
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git required")
    _git_workspace(ws)
    good = {"id": "good", "promotion": "prompt", "target": "prompts/DOCTRINE.md",
            "action": "append", "text": "\n## A fine rule\n", "rationale": "recurred"}

    res, _run_dir, tmpdir = _run_open_pr(ws, tmp_path, monkeypatch, [good], shims_only=("gh",))

    assert res.returncode == 1
    assert "Traceback" not in res.stderr
    assert "not found on PATH" in res.stderr
    branches = subprocess.run(
        [shutil.which("git"), "-C", str(ws), "branch", "--list", "self-improve/*"],
        capture_output=True, text=True,
    ).stdout.strip().lstrip("* ").strip()
    assert branches.startswith("self-improve/"), "the committed branch must survive a push/PR failure"
    assert branches in res.stderr, "the failure message must name the surviving branch"
    assert list(tmpdir.iterdir()) == [], "the temp worktree dir must still be cleaned up"


def test_ladder_table_names_every_promotion_enum_token(ws: Path) -> None:
    # The curator's ladder table must carry the schema's `promotion` token per row —
    # neither the agent nor a customizer should have to guess the mapping.
    schema = json.loads((ws / "schemas/self-improve-proposals.json").read_text(encoding="utf-8"))
    tokens = schema["properties"]["proposals"]["items"]["properties"]["promotion"]["enum"]
    text = (ws / "skills/self-improve-curator/SKILL.md").read_text(encoding="utf-8")
    for token in tokens:
        assert f"`{token}`" in text, f"SKILL.md ladder table is missing the `{token}` enum token"


# --------------------------------------------------------------------------- #
# 5. Retrofit — `cairn new pipeline self-improve` into an existing workspace.
# --------------------------------------------------------------------------- #

_FURNITURE = (
    PIPELINE,
    "agents/curator.yaml",
    "skills/self-improve-curator/SKILL.md",
    "schemas/self-improve-proposals.json",
    VALIDATOR,
    SCRIPT,
    STUB_PROPOSALS,
    "tests/fixtures/proposals/valid-two-promotions.json",
)


def test_new_pipeline_self_improve_retrofits_an_existing_workspace(ws: Path) -> None:
    # Simulate a pre-self-improve workspace: strip the furniture, then retrofit it.
    for rel in _FURNITURE:
        (ws / rel).unlink()
    path = newkit.new_stub("pipeline", "self-improve", ws)
    assert path == ws / PIPELINE
    for rel in _FURNITURE:
        assert (ws / rel).is_file(), f"retrofit did not restore {rel}"
    # The retrofit is plan-valid immediately (this workspace's toml already carries
    # tiers + [tools.gh]; an older toml gets the wiring list from the yaml header).
    build_plan(ws, "self-improve", {}, now=NOW, headless=True)
    # Scripts must stay executable through the copy.
    assert os.access(ws / SCRIPT, os.X_OK)
    assert os.access(ws / VALIDATOR, os.X_OK)


def test_new_pipeline_self_improve_never_clobbers(ws: Path) -> None:
    # The pipeline file itself refuses to overwrite…
    with pytest.raises(FileExistsError):
        newkit.new_stub("pipeline", "self-improve", ws)
    # …and existing companions are left untouched (a customized skill survives).
    skill = ws / "skills/self-improve-curator/SKILL.md"
    (ws / PIPELINE).unlink()
    skill.write_text("customized doctrine\n", encoding="utf-8")
    newkit.new_stub("pipeline", "self-improve", ws)
    assert skill.read_text(encoding="utf-8") == "customized doctrine\n"


def test_retrofit_appends_the_matrix_row_to_an_existing_hello_only_matrix(ws: Path) -> None:
    # tests/matrix.yaml is a SHARED aggregation file: an older workspace already has
    # one (hello row only), so plain no-clobber would silently drop the self-improve
    # row — and with it the standing headless-refusal proof. The retrofit must
    # APPEND the row without touching existing rows.
    for rel in _FURNITURE:
        (ws / rel).unlink()
    (ws / "tests/matrix.yaml").write_text(
        "# my matrix\nhello:\n  - { name: Ada }\n", encoding="utf-8"
    )
    newkit.new_stub("pipeline", "self-improve", ws)
    text = (ws / "tests/matrix.yaml").read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert doc["hello"] == [{"name": "Ada"}], "existing rows must survive untouched"
    assert doc["self-improve"] == [{}], "the self-improve row must be appended"
    assert text.startswith("# my matrix\n"), "the append must not rewrite the file"


def test_retrofit_leaves_a_matrix_that_already_covers_self_improve(ws: Path) -> None:
    for rel in _FURNITURE:
        (ws / rel).unlink()
    custom = "self-improve:\n  - { since: '2026-01-01' }\nhello:\n  - {}\n"
    (ws / "tests/matrix.yaml").write_text(custom, encoding="utf-8")
    newkit.new_stub("pipeline", "self-improve", ws)
    assert (ws / "tests/matrix.yaml").read_text(encoding="utf-8") == custom
