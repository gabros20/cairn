"""The walker — execution semantics (ARCHITECTURE §3).

Behaviour tests against ``cairn.kernel.walk`` through its public surface — ``bootstrap_run``
and ``walk`` — driving real run dirs on disk. Agent steps run through a FAKE Executor that
records its Invocation, writes the artifacts itself, and returns a canned Result; ``run``
steps use the real shell executor. Plans are built by constructing the frozen dataclasses
directly. Each test asserts one observable fact: the exit code, an artifact's validity, a
node's recorded status, or the shape of the trail.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from cairn.executors.shell import ShellExecutor
from cairn.kernel.artifacts import ArtifactDecl
from cairn.kernel.compose import make_composer
from cairn.kernel.config import load_config
from cairn.kernel.expr import parse as parse_expr
from cairn.kernel.gatekit import answer_gate, gate_path
from cairn.kernel.plan import (
    GateNode,
    LoopNode,
    ParallelNode,
    Plan,
    StepNode,
    plan as build_plan,
)
from cairn.kernel.runstate import load_run, node_status
from cairn.kernel.trail import read_trail
from cairn.kernel.types import ExitCode, Result
from cairn.kernel.walk import bootstrap_run, walk

NOW = datetime(2026, 7, 3, 11, 4)
REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Fixtures & builders.
# --------------------------------------------------------------------------- #


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """A minimal workspace: cairn.toml + a nonempty validator + a verdict schema."""
    root = tmp_path / "ws"
    (root / "schemas").mkdir(parents=True)
    (root / "validators").mkdir()
    (root / "cairn.toml").write_text(
        '[workspace]\nname = "test-ws"\ndefault_executor = "fake"\n', encoding="utf-8"
    )
    shutil.copy(REPO / "templates/workspace/validators/nonempty.py", root / "validators/nonempty.py")
    (root / "schemas/verdict.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["verdict"],
                "properties": {"verdict": {"type": "string"}},
            }
        ),
        encoding="utf-8",
    )
    return root


def _nonempty(ws: Path) -> Path:
    return ws / "validators/nonempty.py"


def _verdict_schema(ws: Path) -> Path:
    return ws / "schemas/verdict.json"


def agent_step(
    step_id: str,
    *,
    produces=(),
    needs=(),
    retry=(0, False),
    skippable=False,
    when=None,
    env=(),
) -> StepNode:
    return StepNode(
        id=step_id, kind="agent", agent=None, command=None, args={},
        needs=tuple(needs), needs_optional=(), produces=tuple(produces),
        when_runtime=when, timeout_s=1800, retry=retry, skippable=skippable,
        executor="fake", tier="balanced", effort=None, env=tuple(env), network=False,
    )


def run_step(step_id: str, command: str, *, produces=(), needs=()) -> StepNode:
    return StepNode(
        id=step_id, kind="run", agent=None, command=command, args={},
        needs=tuple(needs), needs_optional=(), produces=tuple(produces),
        when_runtime=None, timeout_s=1800, retry=(0, False), skippable=False,
        executor=None, tier=None, effort=None, env=(), network=False,
    )


def manual_step(step_id: str, command: str, *, produces=()) -> StepNode:
    return StepNode(
        id=step_id, kind="manual", agent=None, command=command, args={},
        needs=(), needs_optional=(), produces=tuple(produces),
        when_runtime=None, timeout_s=1800, retry=(0, False), skippable=False,
        executor=None, tier=None, effort=None, env=(), network=False,
    )


def make_plan(nodes, artifacts, *, params=None, dims=None, resolved_models=None) -> Plan:
    return Plan(
        pipeline="t", version=1, params=params or {}, dims=dims or {},
        run_id_template="t-{date}", nodes=tuple(nodes), artifacts=artifacts,
        guards=(), warnings=[], executor_default="fake",
        resolved_models=resolved_models or {}, skipped=(),
    )


def models_for(*step_ids) -> dict:
    return {sid: ("fake", "fake-model", None) for sid in step_ids}


class FakeExecutor:
    """Records every Invocation; delegates the actual work to an injected callable."""

    name = "fake"
    capabilities = None

    def __init__(self, on_invoke) -> None:
        self.invocations = []
        self._on_invoke = on_invoke

    def doctor(self):
        return []

    def resolve_model(self, tier, effort):
        return ("fake-model", effort)

    def install_guards(self, guards, workspace):
        return None

    def render_workspace(self, workspace):
        return None

    def invoke(self, inv) -> Result:
        self.invocations.append(inv)
        return self._on_invoke(inv, len(self.invocations))


def done_result(exit_code=0, **step) -> Result:
    block = {"status": "done", "summary": "ok", "artifacts": []}
    block.update(step)
    return Result(step=block, exit_code=exit_code, duration_s=1.0)


def _walk(ws, plan, run_dir, executors, *, composer=None, interactive=False, presets=None):
    return walk(
        plan, run_dir,
        workspace_dir=ws, config=load_config(ws), executors=executors,
        composer=composer or (lambda **kw: "PROMPT"),
        interactive=interactive, gate_presets=presets or {}, now=NOW,
    )


# --------------------------------------------------------------------------- #
# 1. Integration — the hello pipeline end to end.
# --------------------------------------------------------------------------- #


def test_hello_pipeline_runs_end_to_end_headless(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    shutil.copytree(REPO / "templates/workspace", ws)
    toml = (ws / "cairn.toml").read_text().replace("{{WORKSPACE_NAME}}", "hello-ws")
    (ws / "cairn.toml").write_text(toml)

    plan = build_plan(ws, "hello", {}, now=NOW)
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")

    code = walk(
        plan, run_dir,
        workspace_dir=ws, config=load_config(ws),
        executors={"shell": ShellExecutor()},
        composer=make_composer(workspace_dir=ws, config=load_config(ws), now=NOW),
        interactive=False, gate_presets={"tone": "friendly"}, now=NOW,
    )

    assert code == ExitCode.OK
    assert json.loads((run_dir / "greeting.json").read_text())["name"] == "world"
    assert (run_dir / "message.txt").read_text().strip() == "Friendly hello, world!"

    tone = json.loads((run_dir / "gates/tone.json").read_text())
    assert tone == {"choice": "friendly", "by": "flag", "at": NOW.isoformat()}

    events = [e["event"] for e in read_trail(run_dir)]
    assert events[0] == "run-start"
    assert events[-1] == "run-done"
    assert load_run(run_dir)["status"] == "done"


# --------------------------------------------------------------------------- #
# 2. Resume — the artifact done-predicate.
# --------------------------------------------------------------------------- #


def test_resume_reruns_only_the_step_whose_artifact_was_deleted(ws: Path, tmp_path: Path) -> None:
    arts = {
        "a1": ArtifactDecl("a1", "a1.txt", validator=_nonempty(ws)),
        "a2": ArtifactDecl("a2", "a2.txt", validator=_nonempty(ws)),
    }
    s1 = agent_step("s1", produces=["a1"])
    s2 = agent_step("s2", produces=["a2"], needs=["a1"])
    plan = make_plan([s1, s2], arts, resolved_models=models_for("s1", "s2"))

    def on_invoke(inv, _n):
        name = "a1.txt" if inv.env["CAIRN_STEP"] == "s1" else "a2.txt"
        (inv.cwd / name).write_text("content")
        return done_result()

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    ex = FakeExecutor(on_invoke)
    assert _walk(ws, plan, run_dir, {"fake": ex}) == ExitCode.OK
    assert len(ex.invocations) == 2

    (run_dir / "a2.txt").unlink()  # only s2 is now not-done
    assert _walk(ws, plan, run_dir, {"fake": ex}) == ExitCode.OK
    assert [i.env["CAIRN_STEP"] for i in ex.invocations] == ["s1", "s2", "s2"]


# --------------------------------------------------------------------------- #
# 3. Agent step — success, learnings, usage.
# --------------------------------------------------------------------------- #


def test_agent_success_emits_learnings_and_usage(ws: Path, tmp_path: Path) -> None:
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"])], arts, resolved_models=models_for("s"))

    def on_invoke(inv, _n):
        (inv.cwd / "a.txt").write_text("hi")
        return done_result(
            learnings=[{"note": "watch the rate limit", "tag": "capture"}],
            metrics={"pages": 3},
            usage={"in_tokens": 10, "out_tokens": 4},
        )

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": FakeExecutor(on_invoke)}) == ExitCode.OK

    events = list(read_trail(run_dir))
    learns = [e for e in events if e["event"] == "learn"]
    assert learns and learns[0]["data"]["note"] == "watch the rate limit"
    done_ev = next(e for e in events if e["event"] == "step-done")
    assert done_ev["data"]["usage"] == {"in_tokens": 10, "out_tokens": 4}
    assert done_ev["data"]["metrics"] == {"pages": 3}
    assert node_status(load_run(run_dir), "s") == "done"


# --------------------------------------------------------------------------- #
# 4. Retry — with feedback, and exhaustion.
# --------------------------------------------------------------------------- #


def test_retry_with_feedback_recomposes_and_succeeds(ws: Path, tmp_path: Path) -> None:
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"], retry=(1, True))], arts, resolved_models=models_for("s"))

    calls = []

    def composer(*, step, plan, run_dir, cycle=None, retry_reasons=()):
        calls.append(list(retry_reasons))
        return "PROMPT"

    def on_invoke(inv, n):
        (inv.cwd / "a.txt").write_text("" if n == 1 else "good")  # empty fails nonempty
        return done_result()

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    ex = FakeExecutor(on_invoke)
    assert _walk(ws, plan, run_dir, {"fake": ex}, composer=composer) == ExitCode.OK

    assert len(ex.invocations) == 2
    assert calls[0] == []            # first attempt: no feedback
    assert calls[1]                  # second attempt: validator reasons injected
    assert (run_dir / "logs/s.r2.prompt.md").is_file()
    events = [e["event"] for e in read_trail(run_dir)]
    assert "retry" in events and events[-1] == "run-done"


def test_retry_exhausted_halts_gate_failed(ws: Path, tmp_path: Path) -> None:
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"], retry=(1, True))], arts, resolved_models=models_for("s"))

    def on_invoke(inv, _n):
        (inv.cwd / "a.txt").write_text("")  # always empty → always invalid
        return done_result()

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    ex = FakeExecutor(on_invoke)
    assert _walk(ws, plan, run_dir, {"fake": ex}) == ExitCode.GATE_FAILED
    assert len(ex.invocations) == 2  # original + one retry
    assert load_run(run_dir)["status"] == "halted"
    assert node_status(load_run(run_dir), "s") == "halted"


# --------------------------------------------------------------------------- #
# 5. STEP status handling — blocked, skippable.
# --------------------------------------------------------------------------- #


def test_blocked_status_halts_gate_failed_with_blockers(ws: Path, tmp_path: Path) -> None:
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"])], arts, resolved_models=models_for("s"))

    def on_invoke(inv, _n):
        return Result(step={"status": "blocked", "summary": "no", "artifacts": [], "blockers": ["auth missing"]}, exit_code=0, duration_s=1.0)

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": FakeExecutor(on_invoke)}) == ExitCode.GATE_FAILED
    fail = next(e for e in read_trail(run_dir) if e["event"] == "step-fail")
    assert fail["data"]["blockers"] == ["auth missing"]


def test_skippable_skip_satisfies_the_node(ws: Path, tmp_path: Path) -> None:
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"], skippable=True)], arts, resolved_models=models_for("s"))

    def on_invoke(inv, _n):
        return Result(step={"status": "skipped", "summary": "nothing to do", "artifacts": []}, exit_code=0, duration_s=1.0)

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": FakeExecutor(on_invoke)}) == ExitCode.OK
    assert not (run_dir / "a.txt").exists()  # produces exempted
    assert node_status(load_run(run_dir), "s") == "skipped"


# --------------------------------------------------------------------------- #
# 6. Timeout & executor failure.
# --------------------------------------------------------------------------- #


def test_exec_timeout_halts_timeout(ws: Path, tmp_path: Path) -> None:
    from cairn.executors.base import ExecTimeout

    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"])], arts, resolved_models=models_for("s"))

    def on_invoke(inv, _n):
        raise ExecTimeout("too slow")

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": FakeExecutor(on_invoke)}) == ExitCode.TIMEOUT
    assert any(e["event"] == "timeout" for e in read_trail(run_dir))


# --------------------------------------------------------------------------- #
# 7. when_runtime — false skips, EvalError halts config.
# --------------------------------------------------------------------------- #


def test_when_runtime_false_skips_step(ws: Path, tmp_path: Path) -> None:
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    step = agent_step("s", produces=["a"], when=parse_expr("params.mode == 'on'"))
    plan = make_plan([step], arts, params={"mode": "off"}, resolved_models=models_for("s"))

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    ex = FakeExecutor(lambda inv, n: done_result())
    assert _walk(ws, plan, run_dir, {"fake": ex}) == ExitCode.OK
    assert ex.invocations == []
    assert node_status(load_run(run_dir), "s") == "skipped"


def test_when_runtime_eval_error_halts_config(ws: Path, tmp_path: Path) -> None:
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    step = agent_step("s", produces=["a"], when=parse_expr("params.nope == 'x'"))
    plan = make_plan([step], arts, params={"mode": "off"}, resolved_models=models_for("s"))

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": FakeExecutor(lambda inv, n: done_result())}) == ExitCode.CONFIG


# --------------------------------------------------------------------------- #
# 8. Gate — through the walker.
# --------------------------------------------------------------------------- #


def test_gate_preset_records_by_flag_and_marks_node_done(ws: Path, tmp_path: Path) -> None:
    gate = GateNode(name="tone", reads=(), ask="?", options=(("a", "A"), ("b", "B")), default="a", when_runtime=None)
    plan = make_plan([gate], {})
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")

    assert _walk(ws, plan, run_dir, {}, presets={"tone": "b"}) == ExitCode.OK
    assert json.loads(gate_path(run_dir, "tone").read_text())["by"] == "flag"
    assert node_status(load_run(run_dir), "tone") == "done"


def test_gate_headless_without_default_halts_needs_human(ws: Path, tmp_path: Path) -> None:
    gate = GateNode(name="tone", reads=(), ask="?", options=(("a", "A"),), default="", when_runtime=None)
    plan = make_plan([gate], {})
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")

    assert _walk(ws, plan, run_dir, {}) == ExitCode.NEEDS_HUMAN
    events = [e["event"] for e in read_trail(run_dir)]
    assert "gate-pending" in events and events[-1] == "run-halt"


def test_gate_tty_reprompts_then_records(ws: Path, tmp_path: Path, monkeypatch) -> None:
    gate = GateNode(name="tone", reads=(), ask="?", options=(("a", "A"), ("b", "B")), default="a", when_runtime=None)
    plan = make_plan([gate], {})
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")

    answers = iter(["nope", "b"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert _walk(ws, plan, run_dir, {}, interactive=True) == ExitCode.OK
    assert json.loads(gate_path(run_dir, "tone").read_text()) == {"choice": "b", "by": "tty", "at": NOW.isoformat()}


def test_externally_answered_gate_is_honored_on_walk(ws: Path, tmp_path: Path) -> None:
    gate = GateNode(name="tone", reads=(), ask="?", options=(("a", "A"),), default="", when_runtime=None)
    plan = make_plan([gate], {})
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    answer_gate(run_dir, "tone", "a")

    assert _walk(ws, plan, run_dir, {}) == ExitCode.OK  # no halt — already answered
    assert node_status(load_run(run_dir), "tone") == "done"


# --------------------------------------------------------------------------- #
# 9. Manual step — headless halts needs-human.
# --------------------------------------------------------------------------- #


def test_manual_headless_halts_needs_human(ws: Path, tmp_path: Path) -> None:
    plan = make_plan([manual_step("m", "do a thing")], {})
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {}) == ExitCode.NEEDS_HUMAN


def test_manual_step_resumes_once_its_produce_exists(ws: Path, tmp_path: Path) -> None:
    art = ArtifactDecl("ctx", "ctx.txt", validator=_nonempty(ws))
    plan = make_plan([manual_step("m", "do it", produces=["ctx"])], {"ctx": art})
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")

    assert _walk(ws, plan, run_dir, {}) == ExitCode.NEEDS_HUMAN  # first pass halts
    (run_dir / "ctx.txt").write_text("operator did it")          # answered out of band
    assert _walk(ws, plan, run_dir, {}) == ExitCode.OK           # resume skips the manual
    assert node_status(load_run(run_dir), "m") == "done"


# --------------------------------------------------------------------------- #
# 10. Loop — until, resume, cap policies.
# --------------------------------------------------------------------------- #


def _review_loop(ws: Path, *, until="approve", on_cap="continue", max_headless=5):
    art = ArtifactDecl("review", "review-c{cycle}.json", schema=_verdict_schema(ws))
    body = agent_step("review", produces=["review"])
    node = LoopNode(
        name="art-review", min=1, max_interactive=3, max_headless=max_headless,
        until=parse_expr(f"artifacts.review.verdict == '{until}'") if until else None,
        on_cap=on_cap, body=(body,), when_runtime=None,
    )
    plan = make_plan([node], {"review": art}, resolved_models=models_for("review"))
    return plan


def _cycle_of(inv) -> int:
    stem = inv.prompt_file.stem  # e.g. review.c2
    for part in stem.split("."):
        if part.startswith("c") and part[1:].isdigit():
            return int(part[1:])
    return 1


def test_loop_stops_when_until_flips_at_cycle_two(ws: Path, tmp_path: Path) -> None:
    plan = _review_loop(ws)
    verdicts = {1: "revise", 2: "approve"}

    def on_invoke(inv, _n):
        k = _cycle_of(inv)
        (inv.cwd / f"review-c{k}.json").write_text(json.dumps({"verdict": verdicts[k]}))
        return done_result()

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    ex = FakeExecutor(on_invoke)
    assert _walk(ws, plan, run_dir, {"fake": ex}) == ExitCode.OK
    assert len(ex.invocations) == 2
    cycles = [e["cycle"] for e in read_trail(run_dir) if e["event"] == "cycle-start"]
    assert cycles == [1, 2]

    # Resume after corrupting cycle-1: only cycle 1 re-runs (cycle 2 still valid → skip).
    (run_dir / "review-c1.json").write_text("{ not json")
    ex2 = FakeExecutor(on_invoke)
    assert _walk(ws, plan, run_dir, {"fake": ex2}) == ExitCode.OK
    assert [_cycle_of(i) for i in ex2.invocations] == [1]


def test_loop_cap_continue_proceeds(ws: Path, tmp_path: Path) -> None:
    plan = _review_loop(ws, on_cap="continue", max_headless=2)  # until never true

    def on_invoke(inv, _n):
        k = _cycle_of(inv)
        (inv.cwd / f"review-c{k}.json").write_text(json.dumps({"verdict": "revise"}))
        return done_result()

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    ex = FakeExecutor(on_invoke)
    assert _walk(ws, plan, run_dir, {"fake": ex}) == ExitCode.OK
    assert len(ex.invocations) == 2  # ran to the cap
    assert any(e["event"] == "loop-capped" for e in read_trail(run_dir))


def test_loop_cap_halt_fails_gate(ws: Path, tmp_path: Path) -> None:
    plan = _review_loop(ws, on_cap="halt", max_headless=2)

    def on_invoke(inv, _n):
        k = _cycle_of(inv)
        (inv.cwd / f"review-c{k}.json").write_text(json.dumps({"verdict": "revise"}))
        return done_result()

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": FakeExecutor(on_invoke)}) == ExitCode.GATE_FAILED


def test_loop_until_none_runs_to_cap(ws: Path, tmp_path: Path) -> None:
    plan = _review_loop(ws, until=None, on_cap="continue", max_headless=3)

    def on_invoke(inv, _n):
        k = _cycle_of(inv)
        (inv.cwd / f"review-c{k}.json").write_text(json.dumps({"verdict": "x"}))
        return done_result()

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    ex = FakeExecutor(on_invoke)
    assert _walk(ws, plan, run_dir, {"fake": ex}) == ExitCode.OK
    assert len(ex.invocations) == 3


# --------------------------------------------------------------------------- #
# 11. Parallel — wait_all halts after siblings finish; trail stays parseable.
# --------------------------------------------------------------------------- #


def test_parallel_wait_all_halts_after_sibling_completes(ws: Path, tmp_path: Path) -> None:
    arts = {
        "ok": ArtifactDecl("ok", "ok.txt", validator=_nonempty(ws)),
        "boom": ArtifactDecl("boom", "boom.txt", validator=_nonempty(ws)),
    }
    ok = agent_step("ok", produces=["ok"])
    boom = agent_step("boom", produces=["boom"])
    node = ParallelNode(name="pair", on_fail="wait_all", steps=(ok, boom), when_runtime=None)
    plan = make_plan([node], arts, resolved_models=models_for("ok", "boom"))

    def on_invoke(inv, _n):
        if inv.env["CAIRN_STEP"] == "ok":
            (inv.cwd / "ok.txt").write_text("done")
        else:
            (inv.cwd / "boom.txt").write_text("")  # invalid
        return done_result()

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    ex = FakeExecutor(on_invoke)
    assert _walk(ws, plan, run_dir, {"fake": ex}) == ExitCode.GATE_FAILED
    assert len(ex.invocations) == 2                       # both ran
    assert (run_dir / "ok.txt").read_text() == "done"     # sibling completed

    # Every trail line parses — the single-writer lock held under threads.
    raw = (run_dir / "trail.jsonl").read_text().splitlines()
    assert all(json.loads(line)["seq"] for line in raw if line.strip())


def test_parallel_fast_halts_gate_failed(ws: Path, tmp_path: Path) -> None:
    arts = {
        "ok": ArtifactDecl("ok", "ok.txt", validator=_nonempty(ws)),
        "boom": ArtifactDecl("boom", "boom.txt", validator=_nonempty(ws)),
    }
    node = ParallelNode(
        name="pair", on_fail="fast",
        steps=(agent_step("boom", produces=["boom"]), agent_step("ok", produces=["ok"])),
        when_runtime=None,
    )
    plan = make_plan([node], arts, resolved_models=models_for("ok", "boom"))

    def on_invoke(inv, _n):
        (inv.cwd / ("boom.txt" if inv.env["CAIRN_STEP"] == "boom" else "ok.txt")).write_text(
            "" if inv.env["CAIRN_STEP"] == "boom" else "done"
        )
        return done_result()

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": FakeExecutor(on_invoke)}) == ExitCode.GATE_FAILED


# --------------------------------------------------------------------------- #
# 12. Environment — deny-by-default.
# --------------------------------------------------------------------------- #


def test_env_is_scrubbed_and_secret_flows_from_dotenv(ws: Path, tmp_path: Path, monkeypatch) -> None:
    (ws / ".env").write_text("# secrets\nSECRET=s3cr3t\n", encoding="utf-8")
    monkeypatch.setenv("CANARY_CAIRN", "leak-me")

    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"], env=["SECRET"])], arts, resolved_models=models_for("s"))

    seen = {}

    def on_invoke(inv, _n):
        seen.update(inv.env)
        (inv.cwd / "a.txt").write_text("ok")
        return done_result()

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": FakeExecutor(on_invoke)}) == ExitCode.OK
    assert "CANARY_CAIRN" not in seen
    assert seen["CAIRN_RUN_DIR"] == str(run_dir)
    assert seen["SECRET"] == "s3cr3t"


def test_missing_secret_halts_config(ws: Path, tmp_path: Path) -> None:
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"], env=["ABSENT_SECRET"])], arts, resolved_models=models_for("s"))
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": FakeExecutor(lambda inv, n: done_result())}) == ExitCode.CONFIG


# --------------------------------------------------------------------------- #
# 13. Lenient run-command rendering (API §2.8).
# --------------------------------------------------------------------------- #


def test_run_command_substitutes_cairn_and_preserves_foreign_braces(ws: Path, tmp_path: Path) -> None:
    art = ArtifactDecl("out", "out.txt", validator=_nonempty(ws))
    cmd = "python3 -c \"open('{artifact:out}','w').write('{params.name} {x}')\""
    plan = make_plan([run_step("write", cmd, produces=["out"])], {"out": art}, params={"name": "world"})

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"shell": ShellExecutor()}) == ExitCode.OK
    assert (run_dir / "out.txt").read_text() == "world {x}"  # {x} passed through verbatim


def test_run_command_unresolved_cairn_ref_halts_config(ws: Path, tmp_path: Path) -> None:
    art = ArtifactDecl("out", "out.txt", validator=_nonempty(ws))
    cmd = "echo {gate:unanswered} > {artifact:out}"
    plan = make_plan([run_step("write", cmd, produces=["out"])], {"out": art})
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"shell": ShellExecutor()}) == ExitCode.CONFIG


# --------------------------------------------------------------------------- #
# 14. bootstrap — -v2 collision suffix.
# --------------------------------------------------------------------------- #


def test_bootstrap_suffixes_v2_on_collision(ws: Path, tmp_path: Path) -> None:
    plan = make_plan([], {})
    root = tmp_path / "runs"
    first = bootstrap_run(ws, plan, now=NOW, runs_root=root)
    second = bootstrap_run(ws, plan, now=NOW, runs_root=root)
    assert first.name == "t-20260703"
    assert second.name == "t-20260703-v2"
