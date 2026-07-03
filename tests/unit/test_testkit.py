"""The workspace test layer — the four suites + record (TESTING.md §4-5).

Every test builds a real tmp workspace from ``templates/workspace`` (plus a couple of
fixture pipelines: an agent step the stub impersonates, a manual step for the exit-6 path,
and a toy guard), authors ``tests/`` fixtures on disk, and drives the public testkit entry
points. Assertions are on observable facts: a suite's pass/fail tally, its failure lines,
and the files ``record_run`` writes.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cairn.executors.shell import ShellExecutor
from cairn.kernel import testkit
from cairn.kernel.artifacts import ValidationResult
from cairn.kernel.compose import make_composer
from cairn.kernel.config import load_config
from cairn.kernel.plan import plan as build_plan
from cairn.kernel.testkit import (
    record_run,
    run_all,
    run_envelope_suite,
    run_guard_suite,
    run_pipeline_suite,
    run_validator_suite,
)
from cairn.kernel.types import ExitCode
from cairn.kernel.walk import bootstrap_run, walk

REPO = Path(__file__).resolve().parents[2]
NOW = testkit._NOW

# A cairn.toml that enables the claude executor (so agent: steps resolve a model) — the
# template ships it commented out.
_CAIRN_TOML = """\
[workspace]
name = "testkit-ws"
doctrine = "prompts/DOCTRINE.md"
runs_dir = "runs"
default_executor = "claude"

[defaults]
step_timeout = "30m"
trail_context = { events = 12, learnings = 5 }

[executors.claude]
enabled = true
[executors.claude.tiers]
reasoning = { model = "opus",   effort = "high" }
balanced  = { model = "sonnet", effort = "medium" }
cheap     = { model = "haiku",  effort = "low" }
"""

# An agent-step pipeline: the stub impersonates `claude` and replays `note.json`.
_AGENTIC = """\
pipeline: agentic
version: 1
run_id: "agentic-{date}"
artifacts:
  note:
    path: note.json
    schema: schemas/greeting.json
guards:
  - name: no-rm
    match: { tool: bash, command: "rm *" }
    check: guards/no-rm.py
    on_error: deny
steps:
  - id: think
    agent: assistant
    produces: [note]
"""

# A manual-step pipeline: headless → exit 6 (NEEDS_HUMAN).
_MANUALP = """\
pipeline: manualp
version: 1
run_id: "manualp-{date}"
artifacts:
  done:
    path: done.txt
    validator: validators/nonempty.py
steps:
  - id: hand
    manual: "Do the thing by hand."
    produces: [done]
"""

_NO_RM = """\
#!/usr/bin/env python3
import json, sys
payload = json.load(sys.stdin)
if "rm -rf" in payload.get("command", ""):
    print("refusing recursive force remove", file=sys.stderr)
    sys.exit(2)
sys.exit(0)
"""


def build_ws(tmp_path: Path) -> Path:
    """A real workspace: the template + an agent pipeline, a manual pipeline, a toy guard."""
    ws = tmp_path / "ws"
    shutil.copytree(REPO / "templates/workspace", ws)
    (ws / "cairn.toml").write_text(_CAIRN_TOML, encoding="utf-8")
    (ws / "pipelines/agentic.yaml").write_text(_AGENTIC, encoding="utf-8")
    (ws / "pipelines/manualp.yaml").write_text(_MANUALP, encoding="utf-8")
    (ws / "guards").mkdir()
    (ws / "guards/no-rm.py").write_text(_NO_RM, encoding="utf-8")
    return ws


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. Validator suite.
# --------------------------------------------------------------------------- #


def test_validator_suite_passes_valid_and_flags_bad_invalid(tmp_path: Path) -> None:
    ws = build_ws(tmp_path)
    # greeting schema requires a string "name".
    write(ws / "tests/fixtures/greeting/valid-basic.json", '{"name": "ada"}')
    write(ws / "tests/fixtures/greeting/invalid-missing.json", '{"pipeline": "x"}')

    result = run_validator_suite(ws)

    assert result.failed == 0, result.failures
    assert result.passed == 2


def test_validator_suite_flags_invalid_fixture_that_actually_passes(tmp_path: Path) -> None:
    ws = build_ws(tmp_path)
    # Named invalid- but it satisfies the schema → the suite must flag it (false-green guard).
    write(ws / "tests/fixtures/greeting/invalid-nope.json", '{"name": "still valid"}')

    result = run_validator_suite(ws)

    assert result.failed == 1
    assert "expected INVALID but passed" in result.failures[0]


def test_validator_suite_counts_reasonless_rejection_as_failure(tmp_path: Path, monkeypatch) -> None:
    ws = build_ws(tmp_path)
    write(ws / "tests/fixtures/greeting/invalid-x.json", '{"name": "ada"}')
    # Force a rejection WITHOUT a reason — normally unreachable (validate always gives a
    # reason), but the rule "a reasonless rejection is itself a failure" must still hold.
    monkeypatch.setattr(testkit, "validate", lambda *a, **k: ValidationResult(ok=False, reasons=[]))

    result = run_validator_suite(ws)

    assert result.failed == 1
    assert "without a reason" in result.failures[0]


def test_validator_suite_no_fixtures_is_a_pass(tmp_path: Path) -> None:
    ws = build_ws(tmp_path)
    result = run_validator_suite(ws)
    assert result.failed == 0 and result.passed == 0
    assert "(no fixtures)" in result.notes


# --------------------------------------------------------------------------- #
# 2. Guard suite.
# --------------------------------------------------------------------------- #


def test_guard_suite_allow_and_deny(tmp_path: Path) -> None:
    ws = build_ws(tmp_path)
    write(ws / "tests/guards/no-rm/allow-ls.json", '{"command": "ls -la"}')
    write(ws / "tests/guards/no-rm/deny-rmrf.json", '{"command": "rm -rf /tmp/x"}')

    result = run_guard_suite(ws)

    assert result.failed == 0, result.failures
    assert result.passed == 2


def test_guard_suite_flags_deny_fixture_that_is_allowed(tmp_path: Path) -> None:
    ws = build_ws(tmp_path)
    # Named deny- but the guard allows it → must be flagged.
    write(ws / "tests/guards/no-rm/deny-wrong.json", '{"command": "ls"}')

    result = run_guard_suite(ws)

    assert result.failed == 1
    assert "expected DENY but was allowed" in result.failures[0]


# --------------------------------------------------------------------------- #
# 3. Pipeline suite (stub runs).
# --------------------------------------------------------------------------- #


def _author_agentic_stub(ws: Path) -> None:
    write(ws / "tests/stubs/agentic/think/note.json", '{"name": "stubbed"}')


def test_pipeline_suite_hello_and_agentic_run_ok(tmp_path: Path) -> None:
    ws = build_ws(tmp_path)
    _author_agentic_stub(ws)
    write(ws / "tests/matrix.yaml", "hello:\n  - {}\nagentic:\n  - {}\n")

    result = run_pipeline_suite(ws)

    assert result.failed == 0, result.failures
    assert result.passed == 2  # both rows to OK — agentic proves the stub impersonates claude


def test_pipeline_suite_expect_6_for_manual_step(tmp_path: Path) -> None:
    ws = build_ws(tmp_path)
    write(ws / "tests/matrix.yaml", "manualp:\n  - { _expect: 6 }\n")

    result = run_pipeline_suite(ws)

    assert result.failed == 0, result.failures
    assert result.passed == 1


def test_pipeline_suite_flags_unmet_expectation(tmp_path: Path) -> None:
    ws = build_ws(tmp_path)
    # manualp actually halts at exit 6, but the row declares OK → mismatch.
    write(ws / "tests/matrix.yaml", "manualp:\n  - {}\n")

    result = run_pipeline_suite(ws)

    assert result.failed == 1
    assert "!= expected 0" in result.failures[0]


def test_pipeline_suite_missing_stub_fails_loudly(tmp_path: Path) -> None:
    ws = build_ws(tmp_path)  # no agentic stub authored
    write(ws / "tests/matrix.yaml", "agentic:\n  - {}\n")

    result = run_pipeline_suite(ws)

    assert result.failed == 1  # stub missing → artifact gate fails → exit != 0


# --------------------------------------------------------------------------- #
# 4. Envelope suite.
# --------------------------------------------------------------------------- #


def test_envelope_suite_update_then_clean_then_drift(tmp_path: Path) -> None:
    ws = build_ws(tmp_path)

    # update=True writes goldens (agentic.think is the only agent step).
    first = run_envelope_suite(ws, update=True)
    assert first.passed == 1
    golden = ws / "tests/envelopes/agentic.think.golden.md"
    assert golden.is_file()

    # A second run without update is clean (deterministic by construction).
    second = run_envelope_suite(ws)
    assert second.failed == 0 and second.passed == 1

    # Perturb the golden → the suite reports drift with a unified diff.
    golden.write_text(golden.read_text() + "\nTAMPERED\n", encoding="utf-8")
    third = run_envelope_suite(ws)
    assert third.failed == 1
    assert "envelope drift" in third.failures[0]


def test_envelope_goldens_are_portable_across_workspace_paths(tmp_path: Path) -> None:
    # A golden written for a workspace at path A must diff clean against an envelope composed
    # for the same workspace checked out at a different path B (CI on another machine).
    ws_a = build_ws(tmp_path / "a")
    ws_b = build_ws(tmp_path / "b")

    assert run_envelope_suite(ws_a, update=True).passed == 1
    # Simulate the committed goldens being checked out under ws_b.
    shutil.copytree(ws_a / "tests/envelopes", ws_b / "tests/envelopes")

    result = run_envelope_suite(ws_b)
    assert result.failed == 0, result.failures
    assert result.passed == 1

    golden = (ws_b / "tests/envelopes/agentic.think.golden.md").read_text()
    assert "<WORKSPACE>" in golden
    assert str(ws_a) not in golden and str(ws_b) not in golden


def test_envelope_suite_no_agent_steps_is_a_pass(tmp_path: Path) -> None:
    ws = build_ws(tmp_path)
    (ws / "pipelines/agentic.yaml").unlink()  # remove the only agent-step pipeline
    result = run_envelope_suite(ws)
    assert result.failed == 0 and result.passed == 0
    assert "(no fixtures)" in result.notes


# --------------------------------------------------------------------------- #
# 5. record_run — harvest a live run, then replay it.
# --------------------------------------------------------------------------- #


def test_record_run_harvests_stubs_and_fixtures_then_pipeline_suite_passes(tmp_path: Path) -> None:
    ws = build_ws(tmp_path)

    # Run hello LIVE (shell steps only — no tokens) to completion.
    plan = build_plan(ws, "hello", {}, now=NOW, headless=True)
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    code = walk(
        plan, run_dir,
        workspace_dir=ws, config=load_config(ws), executors={"shell": ShellExecutor()},
        composer=make_composer(workspace_dir=ws, config=load_config(ws), now=NOW),
        interactive=False, gate_presets={"tone": "friendly"}, now=NOW,
    )
    assert code == ExitCode.OK

    created = record_run(ws, run_dir)

    # Stubs for both producing steps + a single-json fixture for greeting.
    assert (ws / "tests/stubs/hello/greet/greeting.json").is_file()
    assert (ws / "tests/stubs/hello/compose/message.txt").is_file()
    assert (ws / "tests/fixtures/greeting/valid-recorded.json").is_file()
    assert (ws / "tests/stubs/hello/greet/greeting.json") in created

    # The recorded run regression-locks the wiring: the pipeline suite passes from it,
    # and the recorded fixture validates.
    write(ws / "tests/matrix.yaml", "hello:\n  - {}\n")
    pipe = run_pipeline_suite(ws)
    assert pipe.failed == 0, pipe.failures
    val = run_validator_suite(ws)
    assert val.failed == 0, val.failures


def test_record_run_slim_truncates_large_files(tmp_path: Path) -> None:
    ws = build_ws(tmp_path)
    plan = build_plan(ws, "hello", {}, now=NOW, headless=True)
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    walk(
        plan, run_dir,
        workspace_dir=ws, config=load_config(ws), executors={"shell": ShellExecutor()},
        composer=make_composer(workspace_dir=ws, config=load_config(ws), now=NOW),
        interactive=False, gate_presets={"tone": "friendly"}, now=NOW,
    )
    # Bloat the message artifact past the 64 KiB slim threshold.
    (run_dir / "message.txt").write_text("x" * (70 * 1024), encoding="utf-8")

    record_run(ws, run_dir, slim=True)

    recorded = (ws / "tests/stubs/hello/compose/message.txt").read_text()
    assert recorded.startswith("cairn-stub-slim:")
    assert "truncated" in recorded


# --------------------------------------------------------------------------- #
# 6. run_all — the aggregate report.
# --------------------------------------------------------------------------- #


def test_run_all_aggregates_and_reports_ok(tmp_path: Path) -> None:
    ws = build_ws(tmp_path)
    _author_agentic_stub(ws)
    write(ws / "tests/fixtures/greeting/valid-basic.json", '{"name": "ada"}')
    write(ws / "tests/guards/no-rm/deny-rmrf.json", '{"command": "rm -rf /"}')
    write(ws / "tests/matrix.yaml", "agentic:\n  - {}\n")
    run_envelope_suite(ws, update=True)  # seed goldens so the envelope suite is clean

    report = run_all(ws)

    assert set(report.suites) == {"validators", "guards", "pipelines", "envelopes"}
    assert report.ok, {n: s.failures for n, s in report.suites.items()}


def test_run_all_day0_workspace_all_suites_pass(tmp_path: Path) -> None:
    ws = build_ws(tmp_path)  # no tests/ fixtures at all
    report = run_all(ws)
    assert report.ok
    for name in ("validators", "guards", "pipelines"):
        assert "(no fixtures)" in report.suites[name].notes
