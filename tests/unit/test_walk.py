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
import os
import shutil
import threading
import time
from dataclasses import replace
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
from cairn.kernel.walk import _Walk, bootstrap_run, walk

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
    assert tone.pop("mac")  # HMAC-authenticated (gatekeys); asserted end-to-end in test_gatekit
    # gate `at` is trail.format_at's canonical shape (UTC, ms, Z; naive NOW read as UTC).
    assert tone == {"choice": "friendly", "by": "flag", "at": "2026-07-03T11:04:00.000Z"}

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


def test_executor_reported_usage_on_result_wins_over_step_block(ws: Path, tmp_path: Path) -> None:
    # The future json-output path: the executor reports usage on Result (authoritative),
    # outranking a model's self-reported STEP-block usage. Today Result.usage is always None,
    # so this only fires once an executor populates it — the plumbing, tested honestly.
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"])], arts, resolved_models=models_for("s"))

    def on_invoke(inv, _n):
        (inv.cwd / "a.txt").write_text("hi")
        return Result(
            step={"status": "done", "summary": "ok", "usage": {"in_tokens": 1}},
            exit_code=0,
            duration_s=2.5,
            usage={"in_tokens": 999, "out_tokens": 7},
        )

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": FakeExecutor(on_invoke)}) == ExitCode.OK

    done_ev = next(e for e in read_trail(run_dir) if e["event"] == "step-done")
    assert done_ev["data"]["usage"] == {"in_tokens": 999, "out_tokens": 7}
    assert done_ev["data"]["duration_s"] == 2.5


def test_executor_reported_empty_usage_outranks_block_and_is_omitted(ws: Path, tmp_path: Path) -> None:
    # Pinned composed behavior: an executor-reported {} still wins the precedence (the
    # model's STEP-block self-report must not leak through it), and being number-less it
    # is then omitted from step-done entirely — no usage key at all, not the block's dict.
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"])], arts, resolved_models=models_for("s"))

    def on_invoke(inv, _n):
        (inv.cwd / "a.txt").write_text("hi")
        return Result(
            step={"status": "done", "summary": "ok", "usage": {"in_tokens": 1}},
            exit_code=0,
            duration_s=1.0,
            usage={},
        )

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": FakeExecutor(on_invoke)}) == ExitCode.OK

    done_ev = next(e for e in read_trail(run_dir) if e["event"] == "step-done")
    assert "usage" not in done_ev["data"]


def test_created_at_and_node_at_are_z_terminated_utc(ws: Path, tmp_path: Path) -> None:
    # walk.py single-sources the trail's Z-terminated UTC formatter, so run.json's created_at
    # (and every node `at`) parse as tz-aware — no downstream naive-pinning workaround needed.
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"])], arts, resolved_models=models_for("s"))

    def on_invoke(inv, _n):
        (inv.cwd / "a.txt").write_text("hi")
        return done_result()

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    created_at = json.loads((run_dir / "run.json").read_text())["created_at"]
    assert created_at.endswith("Z")
    assert datetime.fromisoformat(created_at).tzinfo is not None  # aware, no naive-pinning

    assert _walk(ws, plan, run_dir, {"fake": FakeExecutor(on_invoke)}) == ExitCode.OK
    node_at = load_run(run_dir)["nodes"]["s"]["at"]
    assert node_at.endswith("Z") and datetime.fromisoformat(node_at).tzinfo is not None


def test_legacy_naive_created_at_still_loads(ws: Path, tmp_path: Path) -> None:
    # Old run dirs carry a naive `created_at` (no Z/offset). Reading tolerance must survive:
    # load_run parses it, and datetime.fromisoformat handles the naive string without crashing.
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"])], arts, resolved_models=models_for("s"))
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")

    doc = json.loads((run_dir / "run.json").read_text())
    doc["created_at"] = "2026-07-03T11:04:00"  # legacy naive stamp
    (run_dir / "run.json").write_text(json.dumps(doc))

    reloaded = load_run(run_dir)
    assert reloaded["created_at"] == "2026-07-03T11:04:00"
    assert datetime.fromisoformat(reloaded["created_at"]).year == 2026


def test_heartbeat_emitted_during_a_slow_step(ws: Path, tmp_path: Path) -> None:
    # A slow step blocks inside invoke() until the walker has emitted at least one heartbeat;
    # a watcher thread releases it once one appears (patched tiny interval — no real sleep).
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"])], arts, resolved_models=models_for("s"))
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")

    release = threading.Event()

    def on_invoke(inv, _n):
        inv.log_path.parent.mkdir(parents=True, exist_ok=True)
        inv.log_path.write_text("working line one\nworking line two\n", encoding="utf-8")
        assert release.wait(timeout=5), "walker emitted no heartbeat while the step was blocked"
        (inv.cwd / "a.txt").write_text("done")
        return done_result()

    def watch() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(e["event"] == "heartbeat" for e in read_trail(run_dir)):
                release.set()
                return
            time.sleep(0.005)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()

    cfg = load_config(ws)
    cfg = replace(cfg, defaults=replace(cfg.defaults, heartbeat_s=0.01))
    code = walk(
        plan, run_dir, workspace_dir=ws, config=cfg,
        executors={"fake": FakeExecutor(on_invoke)},
        composer=lambda **kw: "PROMPT", interactive=False, gate_presets={}, now=NOW,
    )
    watcher.join(timeout=1)

    assert code == ExitCode.OK
    hbs = [e for e in read_trail(run_dir) if e["event"] == "heartbeat"]
    assert hbs, "expected at least one heartbeat event"
    hb = hbs[0]
    assert hb["node"] == "s"
    assert hb["attempt"] == 1
    assert hb["data"]["log_bytes"] > 0
    assert hb["data"]["last_line"] == "working line two"
    assert "elapsed_s" in hb["data"]
    # step-done still lands last for the node — the daemon never outlives the step, and
    # no stale heartbeat sneaks in after it (the stop.is_set() re-check before emit).
    events = list(read_trail(run_dir))
    done_seq = next(e["seq"] for e in events if e["event"] == "step-done" and e["node"] == "s")
    assert all(e["seq"] < done_seq for e in events if e["event"] == "heartbeat")


def test_heartbeat_is_off_by_default_zero_cost(ws: Path, tmp_path: Path) -> None:
    # Unconfigured heartbeat (heartbeat_s is None) emits nothing — no timer thread, no beats.
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"])], arts, resolved_models=models_for("s"))

    def on_invoke(inv, _n):
        (inv.cwd / "a.txt").write_text("hi")
        return done_result()

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert load_config(ws).defaults.heartbeat_s is None
    assert _walk(ws, plan, run_dir, {"fake": FakeExecutor(on_invoke)}) == ExitCode.OK
    assert not [e for e in read_trail(run_dir) if e["event"] == "heartbeat"]


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


def test_recorded_skip_sticks_across_resume(ws: Path, tmp_path: Path) -> None:
    # Ruling 2a: a recorded self-skip is a completed decision — never re-fired on resume.
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"], skippable=True)], arts, resolved_models=models_for("s"))

    ex = FakeExecutor(lambda inv, n: Result(step={"status": "skipped", "summary": "nothing", "artifacts": []}, exit_code=0, duration_s=1.0))
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": ex}) == ExitCode.OK
    assert _walk(ws, plan, run_dir, {"fake": ex}) == ExitCode.OK  # resume
    assert len(ex.invocations) == 1  # NOT re-invoked


def test_recorded_block_reruns_across_resume_even_with_valid_artifact(ws: Path, tmp_path: Path) -> None:
    # Ruling 2b: a halted (blocked) node re-runs regardless of a valid stub on disk.
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"])], arts, resolved_models=models_for("s"))

    def on_invoke(inv, n):
        (inv.cwd / "a.txt").write_text("valid stub")  # writes a VALID artifact each time
        if n == 1:
            return Result(step={"status": "blocked", "summary": "no", "artifacts": [], "blockers": ["x"]}, exit_code=0, duration_s=1.0)
        return done_result()

    ex = FakeExecutor(on_invoke)
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": ex}) == ExitCode.GATE_FAILED
    assert node_status(load_run(run_dir), "s") == "halted"

    assert _walk(ws, plan, run_dir, {"fake": ex}) == ExitCode.OK  # resume re-runs despite valid artifact
    assert len(ex.invocations) == 2
    assert node_status(load_run(run_dir), "s") == "done"


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
    tone = json.loads(gate_path(run_dir, "tone").read_text())
    assert tone.pop("mac")  # HMAC-authenticated decision (gatekeys)
    assert tone == {"choice": "b", "by": "tty", "at": "2026-07-03T11:04:00.000Z"}


def test_externally_answered_gate_is_honored_on_walk(ws: Path, tmp_path: Path) -> None:
    gate = GateNode(name="tone", reads=(), ask="?", options=(("a", "A"),), default="", when_runtime=None)
    plan = make_plan([gate], {})
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    answer_gate(run_dir, "tone", "a")

    assert _walk(ws, plan, run_dir, {}) == ExitCode.OK  # no halt — already answered
    assert node_status(load_run(run_dir), "tone") == "done"


def test_forged_gate_file_halts_needs_human_and_emits_gate_tamper(ws: Path, tmp_path: Path) -> None:
    # End-to-end through the real walker + trail: an agent-forged (unsigned) decision file must
    # NOT skip a defaultless gate — it halts needs-human and lands a gate-tamper trail event.
    gate = GateNode(name="tone", reads=(), ask="?", options=(("a", "A"),), default="", when_runtime=None)
    plan = make_plan([gate], {})
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    gp = gate_path(run_dir, "tone")
    gp.parent.mkdir(parents=True, exist_ok=True)
    gp.write_text(json.dumps({"choice": "a", "by": "tty"}), encoding="utf-8")  # forged, no MAC

    assert _walk(ws, plan, run_dir, {}) == ExitCode.NEEDS_HUMAN  # forge rejected, fail safe
    events = [e["event"] for e in read_trail(run_dir)]
    assert "gate-tamper" in events and events[-1] == "run-halt"


def test_post_resolution_forge_does_not_reach_when_control_flow(ws: Path, tmp_path: Path) -> None:
    # F-SEC-1: a gate resolves to a signed "no"; a later compromised step overwrites the decision
    # file with a forged {"choice":"yes"}. A downstream `when: gates.deploy.choice == "yes"` step
    # must NOT run — the `when:` reader verifies the MAC too, so the forge is rejected (gate-tamper)
    # and the walk halts instead of shipping. This is the exact bypass F-SEC-1 flagged.
    gate = GateNode(
        name="deploy", reads=(), ask="?",
        options=(("yes", "ship"), ("no", "hold")), default="no", when_runtime=None,
    )
    forge = agent_step("forge", produces=["fx"])
    shipit = agent_step("shipit", produces=["fy"], when=parse_expr("gates.deploy.choice == 'yes'"))
    arts = {
        "fx": ArtifactDecl("fx", "fx.json", validator=_nonempty(ws)),
        "fy": ArtifactDecl("fy", "fy.json", validator=_nonempty(ws)),
    }
    plan = make_plan([gate, forge, shipit], arts, resolved_models=models_for("forge", "shipit"))
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    answer_gate(run_dir, "deploy", "no")  # the human said NO — signed

    ran: list[str] = []

    def on_invoke(inv, _n):
        step = inv.env["CAIRN_STEP"]
        ran.append(step)
        if step == "forge":  # the compromised step forges the human's decision to "yes", no MAC
            gate_path(run_dir, "deploy").write_text(
                json.dumps({"choice": "yes", "by": "tty"}), encoding="utf-8"
            )
            (inv.cwd / "fx.json").write_text('{"ok":1}', encoding="utf-8")
        else:  # shipit — must never run
            (inv.cwd / "fy.json").write_text('{"shipped":1}', encoding="utf-8")
        return done_result()

    code = _walk(ws, plan, run_dir, {"fake": FakeExecutor(on_invoke)})

    assert code == ExitCode.CONFIG               # the forged `when:` eval halts the walk
    assert "shipit" not in ran                   # the deploy step never ran on a forged "yes"
    assert not (run_dir / "fy.json").exists()
    assert "gate-tamper" in [e["event"] for e in read_trail(run_dir)]


# --------------------------------------------------------------------------- #
# 9. Manual step — headless halts needs-human.
# --------------------------------------------------------------------------- #


def test_manual_headless_halts_needs_human(ws: Path, tmp_path: Path) -> None:
    plan = make_plan([manual_step("m", "do a thing")], {})
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {}) == ExitCode.NEEDS_HUMAN


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
def test_gate_tty_interrupt_halts_needs_human(ws: Path, tmp_path: Path, monkeypatch, exc) -> None:
    gate = GateNode(name="tone", reads=(), ask="?", options=(("a", "A"),), default="a", when_runtime=None)
    plan = make_plan([gate], {})
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")

    def interrupt(*_a):
        raise exc()

    monkeypatch.setattr("builtins.input", interrupt)
    assert _walk(ws, plan, run_dir, {}, interactive=True) == ExitCode.NEEDS_HUMAN
    assert load_run(run_dir)["status"] == "halted"
    assert any(e["event"] == "run-halt" for e in read_trail(run_dir))


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
def test_manual_tty_interrupt_halts_needs_human(ws: Path, tmp_path: Path, monkeypatch, exc) -> None:
    plan = make_plan([manual_step("m", "do it")], {})
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")

    def interrupt(*_a):
        raise exc()

    monkeypatch.setattr("builtins.input", interrupt)
    assert _walk(ws, plan, run_dir, {}, interactive=True) == ExitCode.NEEDS_HUMAN
    assert load_run(run_dir)["status"] == "halted"
    assert any(e["event"] == "run-halt" for e in read_trail(run_dir))


def test_manual_step_resumes_once_its_produce_exists(ws: Path, tmp_path: Path) -> None:
    # Schema-validated (in-process) artifact — no subprocess validator in the resume-decision
    # path, so the skip verdict is a pure function of disk state, deterministic under any
    # ordering/load. The behaviour under test is the halt→operator→resume-skip semantics.
    art = ArtifactDecl("ctx", "ctx.json", schema=_verdict_schema(ws))
    plan = make_plan([manual_step("m", "do it", produces=["ctx"])], {"ctx": art})
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")

    assert _walk(ws, plan, run_dir, {}) == ExitCode.NEEDS_HUMAN  # first pass halts
    # The needs-human halt records the NODE as "running" (not "halted") — that is what lets a
    # resume satisfy it via the artifact predicate instead of force-re-running it.
    assert node_status(load_run(run_dir), "m") == "running"

    (run_dir / "ctx.json").write_text(json.dumps({"verdict": "operator did it"}))  # answered out of band
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


def test_completed_cycles_is_bounded_by_the_loop_cap(ws: Path, tmp_path: Path) -> None:
    """codex-F3 runtime backstop: _completed_cycles' cycle-discovery loop must not spin
    forever on a cycle-INVARIANT produce (one whose path template omits {cycle}, so it
    renders byte-identically — and validates identically — for cycle 1, 2, 3, ...).

    cairn.kernel.plan rejects this shape at plan time for a new loop-body produce; this
    test reaches _completed_cycles directly (bypassing that check, as a hand-built or
    otherwise-bypassed Plan would) to prove the walker itself cannot hang on it either.
    """
    art = ArtifactDecl("review", "review.json", schema=_verdict_schema(ws))  # no {cycle}
    body = agent_step("review", produces=["review"])
    node = LoopNode(
        name="art-review", min=1, max_interactive=3, max_headless=2,
        until=parse_expr("artifacts.review.verdict == 'approve'"),
        on_cap="continue", body=(body,), when_runtime=None,
    )
    plan_obj = make_plan([node], {"review": art}, resolved_models=models_for("review"))
    run_dir = bootstrap_run(ws, plan_obj, now=NOW, runs_root=tmp_path / "runs")
    # One file, at the fixed (cycle-invariant) path — it validates for every k, so nothing
    # would ever stop a `while True` cycle-discovery loop.
    (run_dir / "review.json").write_text(json.dumps({"verdict": "revise"}))

    w = _Walk(
        plan=plan_obj, run_dir=run_dir, workspace_dir=ws, config=load_config(ws),
        executors={}, composer=lambda **kw: "PROMPT", interactive=False,
        gate_presets={}, now=NOW, timeout=30,
    )
    assert w._completed_cycles(node) == 2  # returns at max_headless, not unbounded


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


def test_identity_env_passes_through_for_keychain_auth(ws: Path, tmp_path: Path, monkeypatch) -> None:
    # USER/LOGNAME must reach the child: on macOS a CLI's Keychain lookup (how `claude`
    # finds its OAuth credential) needs USER — without it the child reports "Not logged in".
    monkeypatch.setenv("USER", "tamas")
    monkeypatch.setenv("LOGNAME", "tamas")
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"])], arts, resolved_models=models_for("s"))

    seen = {}

    def on_invoke(inv, _n):
        seen.update(inv.env)
        (inv.cwd / "a.txt").write_text("ok")
        return done_result()

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": FakeExecutor(on_invoke)}) == ExitCode.OK
    assert seen["USER"] == "tamas"
    assert seen["LOGNAME"] == "tamas"


class _UniversalEnv(dict):
    """A parent env that 'contains' EVERY key (synthesizing a value on demand).

    Used to pin the walker's system-env passthrough set exactly: any future widening of
    the baseline tuple pulls its new key out of this mapping and into the invocation env,
    breaking the exact-set assertion below — a silent addition can never pass green.
    """

    def __contains__(self, key: object) -> bool:
        return True

    def __getitem__(self, key):
        return super().__getitem__(key) if super().__contains__(key) else f"parent-{key}"

    def get(self, key, default=None):
        return self[key]


def test_system_env_passthrough_is_exactly_the_pinned_set(ws: Path, tmp_path: Path, monkeypatch) -> None:
    # SECURITY §1.2: the env baseline is deny-by-default and a security boundary. The parent
    # env here "contains" every key, so the invocation env must equal EXACTLY the allowed
    # system set + the CAIRN_* mechanics — widening the passthrough (e.g. a secret-bearing
    # var slipping in) fails this test loudly and forces a deliberate decision.
    env = _UniversalEnv()
    env["XDG_STATE_HOME"] = str(tmp_path / "xdg")  # keep the bootstrap key-mint hermetic
    monkeypatch.setattr(os, "environ", env)
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"])], arts, resolved_models=models_for("s"))

    captured: dict = {}

    def on_invoke(inv, _n):
        captured["env"] = dict(inv.env)
        (inv.cwd / "a.txt").write_text("ok")
        return done_result()

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": FakeExecutor(on_invoke)}) == ExitCode.OK
    assert set(captured["env"]) == {
        # the allowed system passthrough — identity + locale + tmp, nothing more
        "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME",
        # the cairn mechanics the walker always injects
        "CAIRN_RUN_DIR", "CAIRN_STEP", "CAIRN_WORKSPACE", "CLAUDE_PROJECT_DIR",
    }


def test_absent_baseline_vars_are_omitted_not_none(ws: Path, tmp_path: Path, monkeypatch) -> None:
    # Absence tolerance: a parent env lacking USER/LOGNAME/TMPDIR/… simply omits those keys
    # from the child env — never a None value, never a crash.
    monkeypatch.setattr(
        os, "environ",
        {"PATH": "/usr/bin:/bin", "HOME": "/tmp/h", "XDG_STATE_HOME": str(tmp_path / "xdg")},
    )
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"])], arts, resolved_models=models_for("s"))

    captured: dict = {}

    def on_invoke(inv, _n):
        captured["env"] = dict(inv.env)
        (inv.cwd / "a.txt").write_text("ok")
        return done_result()

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": FakeExecutor(on_invoke)}) == ExitCode.OK
    env = captured["env"]
    assert set(env) == {
        "PATH", "HOME",
        "CAIRN_RUN_DIR", "CAIRN_STEP", "CAIRN_WORKSPACE", "CLAUDE_PROJECT_DIR",
    }
    assert None not in env.values()


def test_dotenv_strips_quotes_and_tolerates_export(ws: Path, tmp_path: Path) -> None:
    (ws / ".env").write_text('export QUOTED="q u o t e d"\nBARE=plain\n', encoding="utf-8")
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan(
        [agent_step("s", produces=["a"], env=["QUOTED", "BARE"])], arts, resolved_models=models_for("s")
    )

    seen = {}

    def on_invoke(inv, _n):
        seen.update(inv.env)
        (inv.cwd / "a.txt").write_text("ok")
        return done_result()

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": FakeExecutor(on_invoke)}) == ExitCode.OK
    assert seen["QUOTED"] == "q u o t e d"  # surrounding quotes stripped
    assert seen["BARE"] == "plain"          # export prefix tolerated


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


def test_run_command_nonzero_exit_fails_even_when_produces_validate(ws: Path, tmp_path: Path) -> None:
    art = ArtifactDecl("out", "out.txt", validator=_nonempty(ws))
    cmd = "python3 -c \"open('{artifact:out}','w').write('ok')\" ; exit 3"
    plan = make_plan([run_step("write", cmd, produces=["out"])], {"out": art})
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")

    assert _walk(ws, plan, run_dir, {"shell": ShellExecutor()}) == ExitCode.GATE_FAILED
    assert (run_dir / "out.txt").read_text() == "ok"  # the produce IS valid — exit code still fails it


# --------------------------------------------------------------------------- #
# 13a2. Failure taxonomy — typed spawn errors, agent exit codes, no run left "running".
# --------------------------------------------------------------------------- #


def test_agent_step_absent_binary_halts_executor_not_traceback(ws: Path, tmp_path: Path) -> None:
    """codex-F14/claude-F4: a missing executable is a typed run-halt at exit 4, not a
    traceback that leaves run.json stuck "running"."""
    from cairn.executors.base import run_process

    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"])], arts, resolved_models=models_for("s"))

    class SpawnFailExecutor(FakeExecutor):
        def __init__(self) -> None:
            super().__init__(on_invoke=None)

        def invoke(self, inv):
            self.invocations.append(inv)
            # Exercise the real spawn path (base.run_process), not a canned Result — this
            # is the same OSError→ExecutorSpawnError conversion base.py:115 performs.
            run_process(
                ["cairn-definitely-not-a-real-binary-xyz"],
                stdin_text=None,
                env=inv.env,
                cwd=inv.cwd,
                timeout_s=inv.timeout_s,
                log_path=inv.log_path,
            )
            raise AssertionError("unreachable — run_process must raise on a missing binary")

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    assert _walk(ws, plan, run_dir, {"fake": SpawnFailExecutor()}) == ExitCode.EXECUTOR
    assert load_run(run_dir)["status"] == "halted"
    halt = next(e for e in read_trail(run_dir) if e["event"] == "run-halt")
    assert halt["data"]["exit_code"] == int(ExitCode.EXECUTOR)


def test_agent_nonzero_exit_with_auth_signature_halts_executor_and_skips_retries(
    ws: Path, tmp_path: Path
) -> None:
    """codex-F7/grok-F11/claude-F3: an agent CLI that exits non-zero with an auth/executor
    signature in its output is a step-fail → run-halt at exit 4 — even with a would-be-valid
    artifact — and burns none of the retry budget (retrying auth failure is pure waste)."""
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"], retry=(2, True))], arts, resolved_models=models_for("s"))

    def on_invoke(inv, _n):
        inv.log_path.parent.mkdir(parents=True, exist_ok=True)
        inv.log_path.write_text("Error: not logged in. Please run `claude /login`.\n", encoding="utf-8")
        (inv.cwd / "a.txt").write_text("would-be-valid", encoding="utf-8")  # a valid artifact
        return Result(step={"status": "done", "summary": "ok", "artifacts": []}, exit_code=1, duration_s=1.0)

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    ex = FakeExecutor(on_invoke)
    assert _walk(ws, plan, run_dir, {"fake": ex}) == ExitCode.EXECUTOR
    assert len(ex.invocations) == 1  # retry budget (2) untouched — auth failure isn't retried
    events = [e["event"] for e in read_trail(run_dir)]
    assert "step-fail" in events
    assert events[-1] == "run-halt"


def test_agent_nonzero_exit_without_auth_signature_still_retries(ws: Path, tmp_path: Path) -> None:
    """The content-invalid retry path (walk.py ~469) stays intact: a non-zero exit with no
    auth/executor signature in the output is a content failure, not an executor failure."""
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"], retry=(1, True))], arts, resolved_models=models_for("s"))

    def on_invoke(inv, _n):
        inv.log_path.parent.mkdir(parents=True, exist_ok=True)
        inv.log_path.write_text("some generic tool crash, nothing auth-related\n", encoding="utf-8")
        (inv.cwd / "a.txt").write_text("", encoding="utf-8")  # always empty → always invalid
        return Result(step={"status": "done", "summary": "ok", "artifacts": []}, exit_code=1, duration_s=1.0)

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    ex = FakeExecutor(on_invoke)
    assert _walk(ws, plan, run_dir, {"fake": ex}) == ExitCode.GATE_FAILED
    assert len(ex.invocations) == 2  # original + one retry — content path preserved


def test_agent_nonzero_exit_auth_signature_mid_transcript_does_not_halt(ws: Path, tmp_path: Path) -> None:
    """M1/L1: classification reads only the log TAIL (walk.py's ``_tail_text``), not the whole
    transcript. An auth-looking substring buried early — a legitimate (possibly long) coding
    step's output, or one whose task is literally about auth — must not short-circuit
    retries; only a signature in the CLOSING lines (where a real vendor CLI auth failure
    actually prints, right before it exits) does."""
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"], retry=(1, True))], arts, resolved_models=models_for("s"))

    def on_invoke(inv, _n):
        inv.log_path.parent.mkdir(parents=True, exist_ok=True)
        early_auth_sign = "Error: not logged in. Please run `claude /login`.\n"
        # Pad well past the 8 KiB tail window so the early sign falls outside it.
        padding = "benign log line filling space\n" * 400
        clean_tail = "some generic tool crash, nothing auth-related at the tail\n"
        inv.log_path.write_text(early_auth_sign + padding + clean_tail, encoding="utf-8")
        (inv.cwd / "a.txt").write_text("", encoding="utf-8")  # always empty → always invalid
        return Result(step={"status": "done", "summary": "ok", "artifacts": []}, exit_code=1, duration_s=1.0)

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    ex = FakeExecutor(on_invoke)
    assert _walk(ws, plan, run_dir, {"fake": ex}) == ExitCode.GATE_FAILED
    assert len(ex.invocations) == 2  # original + one retry — mid-transcript sign is ignored


def test_unexpected_exception_does_not_leave_run_running(ws: Path, tmp_path: Path) -> None:
    """claude-F4 belt: any non-_Halt exception escaping the walk still marks the run halted
    (never "running") and records a run-halt, then re-raises so the CLI exits non-zero."""
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"])], arts, resolved_models=models_for("s"))

    def on_invoke(inv, _n):
        raise RuntimeError("kaboom — not a CairnError, not an ExecTimeout")

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    with pytest.raises(RuntimeError, match="kaboom"):
        _walk(ws, plan, run_dir, {"fake": FakeExecutor(on_invoke)})

    assert load_run(run_dir)["status"] != "running"
    assert load_run(run_dir)["status"] == "halted"
    halt = next(e for e in read_trail(run_dir) if e["event"] == "run-halt")
    assert "internal-error" in halt["data"]["reason"]


def test_install_guards_exception_does_not_leave_run_running(ws: Path, tmp_path: Path) -> None:
    """The belt covers the whole run body, not just the node loop: an exception raised
    BEFORE the first node ever dispatches (e.g. a plugin's install_guards, run-start/plan
    emit) must still halt the run and record run-halt — a belt scoped only around the
    `for node in self.plan.nodes` loop misses this call site entirely and reproduces the
    exact "stuck running" bug sub-change 3 exists to close."""
    arts = {"a": ArtifactDecl("a", "a.txt", validator=_nonempty(ws))}
    plan = make_plan([agent_step("s", produces=["a"])], arts, resolved_models=models_for("s"))

    def unreachable(inv, _n):
        raise AssertionError("unreachable — install_guards must raise before any node dispatches")

    class GuardBoomExecutor(FakeExecutor):
        def install_guards(self, guards, workspace):
            raise RuntimeError("guard plugin exploded")

    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    with pytest.raises(RuntimeError, match="guard plugin exploded"):
        _walk(ws, plan, run_dir, {"fake": GuardBoomExecutor(unreachable)})

    assert load_run(run_dir)["status"] != "running"
    assert load_run(run_dir)["status"] == "halted"
    halt = next(e for e in read_trail(run_dir) if e["event"] == "run-halt")
    assert "internal-error" in halt["data"]["reason"]


# --------------------------------------------------------------------------- #
# 13b. Redaction — declared secrets scrubbed from the log AND the trail (SECURITY §1.3).
# --------------------------------------------------------------------------- #


def test_declared_secret_is_redacted_from_step_log_and_trail(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "cairn.toml").write_text(
        '[workspace]\nname = "sec-ws"\ndefault_executor = "shell"\n\n'
        '[secrets]\nTOKEN = { needed_by = ["leak"] }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("TOKEN", "sk-live-DEADBEEF")

    # A run step that (a) echoes the secret from its env to stdout → logs/leak.log, and
    # (b) smuggles it into a STEP learning note → a `learn` trail event. Both must be scrubbed.
    command = (
        'echo "token=$TOKEN"; '
        'echo "<<<STEP {\\"status\\":\\"done\\",\\"summary\\":\\"ok\\",'
        '\\"artifacts\\":[],\\"learnings\\":[{\\"note\\":\\"saw $TOKEN\\"}]} STEP>>>"'
    )
    step = StepNode(
        id="leak", kind="run", agent=None, command=command, args={},
        needs=(), needs_optional=(), produces=(),
        when_runtime=None, timeout_s=1800, retry=(0, False), skippable=False,
        executor=None, tier=None, effort=None, env=("TOKEN",), network=False,
    )
    plan = make_plan([step], {})
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")

    code = _walk(ws, plan, run_dir, {"shell": ShellExecutor()})
    assert code == ExitCode.OK  # redaction never fails or slows the run

    log_text = (run_dir / "logs" / "leak.log").read_text()
    assert "sk-live-DEADBEEF" not in log_text
    assert "∎REDACTED:TOKEN∎" in log_text

    trail_text = (run_dir / "trail.jsonl").read_text()
    assert "sk-live-DEADBEEF" not in trail_text  # the learn event was scrubbed
    assert "∎REDACTED:TOKEN∎" in trail_text


def test_tee_sinks_are_closed_when_trailwriter_init_fails(ws: Path, tmp_path: Path, monkeypatch) -> None:
    # Sinks are built before the TrailWriter; if its construction raises, the webhook daemon
    # threads must still be closed — never leaked past the walk.
    import cairn.kernel.walk as walkmod

    closed: list[bool] = []

    class RecordingSink:
        def emit(self, event: dict) -> None:
            pass

        def close(self) -> None:
            closed.append(True)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(walkmod, "build_tee_sinks", lambda spec: [RecordingSink()])
    monkeypatch.setattr("cairn.kernel.trail.TrailWriter.__init__", boom)

    plan = make_plan([run_step("noop", "true")], {})
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")
    with pytest.raises(OSError):
        _walk(ws, plan, run_dir, {"shell": ShellExecutor()})
    assert closed == [True]


def test_no_redaction_when_secret_is_unset(tmp_path: Path, monkeypatch) -> None:
    # A declared-but-unset secret resolves to nothing → no redactor → output is verbatim.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "cairn.toml").write_text(
        '[workspace]\nname = "sec-ws"\ndefault_executor = "shell"\n\n'
        '[secrets]\nTOKEN = { needed_by = ["say"] }\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("TOKEN", raising=False)

    step = run_step("say", 'echo "plain output ∎REDACTED:TOKEN∎-shaped but real"')
    plan = make_plan([step], {})
    run_dir = bootstrap_run(ws, plan, now=NOW, runs_root=tmp_path / "runs")

    assert _walk(ws, plan, run_dir, {"shell": ShellExecutor()}) == ExitCode.OK
    # Nothing resolved → the line is teed verbatim (the marker-shaped literal survives untouched).
    assert "plain output" in (run_dir / "logs" / "say.log").read_text()


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
