"""The stub executor — L1 replay semantics (TESTING.md §5).

Behaviour tests against ``cairn.executors.stub.StubExecutor`` through its public protocol:
build an :class:`Invocation` pointing at a real run dir + a ``tests/stubs`` tree on disk,
call ``invoke``, and assert one observable fact — which files landed in the run dir, and the
shape of the returned STEP. The identity derivations (pipeline from run.json, cycle from the
log-path suffix, stubs_root from CAIRN_WORKSPACE) are exercised the way the walker drives them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairn.executors.stub import StubExecutor
from cairn.kernel.types import Invocation


# --------------------------------------------------------------------------- #
# Builders.
# --------------------------------------------------------------------------- #


def make_run_dir(tmp_path: Path, pipeline: str = "hello") -> Path:
    run_dir = tmp_path / "run"
    (run_dir / "logs").mkdir(parents=True)
    # The stub reads run.json directly (not load_run), so a minimal manifest suffices.
    (run_dir / "run.json").write_text(json.dumps({"pipeline": pipeline}), encoding="utf-8")
    return run_dir


def write_stub(stubs_root: Path, pipeline: str, step_dir: str, files: dict[str, str]) -> Path:
    d = stubs_root / pipeline / step_dir
    d.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


def make_inv(run_dir: Path, workspace: Path, step: str, *, cycle: int | None = None,
             attempt: int = 1, extra_env: dict | None = None) -> Invocation:
    stem = step
    if attempt > 1:
        stem += f".r{attempt}"
    if cycle is not None:
        stem += f".c{cycle}"
    env = {"CAIRN_STEP": step, "CAIRN_WORKSPACE": str(workspace)}
    if extra_env:
        env.update(extra_env)
    return Invocation(
        prompt_file=run_dir / "logs" / f"{stem}.prompt.md",
        model="stub", effort=None, cwd=run_dir, env=env, timeout_s=60,
        log_path=run_dir / "logs" / f"{stem}.log", return_schema=run_dir / ".cairn/step-return.json",
    )


# --------------------------------------------------------------------------- #
# 1. Replay — copies the tree, synthesizes a done STEP.
# --------------------------------------------------------------------------- #


def test_invoke_copies_tree_and_synthesizes_done_step(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    run_dir = make_run_dir(tmp_path, "hello")
    write_stub(ws / "tests/stubs", "hello", "greet", {"greeting.json": '{"name": "world"}'})

    result = StubExecutor().invoke(make_inv(run_dir, ws, "greet"))

    assert result.exit_code == 0
    assert (run_dir / "greeting.json").read_text() == '{"name": "world"}'
    assert result.step == {
        "status": "done",
        "summary": "stub replay of greet",
        "artifacts": ["greeting.json"],
    }


def test_invoke_copies_nested_subtree(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    run_dir = make_run_dir(tmp_path)
    write_stub(ws / "tests/stubs", "hello", "build",
               {"blueprints/home.json": "{}", "blueprints/about.json": "{}"})

    result = StubExecutor().invoke(make_inv(run_dir, ws, "build"))

    assert (run_dir / "blueprints/home.json").is_file()
    assert (run_dir / "blueprints/about.json").is_file()
    assert result.step["artifacts"] == ["blueprints/about.json", "blueprints/home.json"]


# --------------------------------------------------------------------------- #
# 2. Cycle preference — .cK dir wins over the bare dir.
# --------------------------------------------------------------------------- #


def test_cycle_suffixed_dir_is_preferred(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    run_dir = make_run_dir(tmp_path)
    write_stub(ws / "tests/stubs", "hello", "review", {"review.json": '{"verdict": "revise"}'})
    write_stub(ws / "tests/stubs", "hello", "review.c2", {"review.json": '{"verdict": "approve"}'})

    StubExecutor().invoke(make_inv(run_dir, ws, "review", cycle=2))

    assert json.loads((run_dir / "review.json").read_text())["verdict"] == "approve"


def test_bare_dir_used_when_no_cycle_dir(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    run_dir = make_run_dir(tmp_path)
    write_stub(ws / "tests/stubs", "hello", "review", {"review.json": '{"verdict": "revise"}'})

    StubExecutor().invoke(make_inv(run_dir, ws, "review", cycle=3))

    assert json.loads((run_dir / "review.json").read_text())["verdict"] == "revise"


def test_cycle_is_parsed_from_log_path_suffix(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    run_dir = make_run_dir(tmp_path)
    write_stub(ws / "tests/stubs", "hello", "review.c2", {"c2.txt": "two"})
    # A retry AND a cycle suffix — the .c2 must still be found past the .r2.
    StubExecutor().invoke(make_inv(run_dir, ws, "review", cycle=2, attempt=2))
    assert (run_dir / "c2.txt").is_file()


# --------------------------------------------------------------------------- #
# 3. Sidecar _step.json — used verbatim, never copied.
# --------------------------------------------------------------------------- #


def test_sidecar_step_json_is_used_verbatim_and_not_copied(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    run_dir = make_run_dir(tmp_path)
    canned = {"status": "done", "summary": "hand-authored", "artifacts": ["site-map.json"],
              "metrics": {"pages": 19}}
    d = write_stub(ws / "tests/stubs", "hello", "capture", {"site-map.json": "{}"})
    (d / "_step.json").write_text(json.dumps(canned), encoding="utf-8")

    result = StubExecutor().invoke(make_inv(run_dir, ws, "capture"))

    assert result.step == canned
    assert not (run_dir / "_step.json").exists()  # sidecar is metadata, not an artifact
    assert (run_dir / "site-map.json").is_file()


# --------------------------------------------------------------------------- #
# 4. Missing stub dir — loud failure (step None, exit 1).
# --------------------------------------------------------------------------- #


def test_missing_stub_dir_returns_none_step_exit_1(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "tests/stubs").mkdir(parents=True)
    run_dir = make_run_dir(tmp_path)

    result = StubExecutor().invoke(make_inv(run_dir, ws, "nope"))

    assert result.step is None
    assert result.exit_code == 1


# --------------------------------------------------------------------------- #
# 5. Pipeline derivation & stubs_root resolution.
# --------------------------------------------------------------------------- #


def test_pipeline_comes_from_run_json(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    run_dir = make_run_dir(tmp_path, pipeline="brease-rebuild")
    write_stub(ws / "tests/stubs", "brease-rebuild", "capture", {"x.json": "{}"})
    # Same step id under a different pipeline must NOT match.
    write_stub(ws / "tests/stubs", "hello", "capture", {"wrong.json": "{}"})

    StubExecutor().invoke(make_inv(run_dir, ws, "capture"))

    assert (run_dir / "x.json").is_file()
    assert not (run_dir / "wrong.json").exists()


def test_ctor_stubs_root_overrides_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    other = tmp_path / "elsewhere"
    run_dir = make_run_dir(tmp_path)
    write_stub(other, "hello", "greet", {"from-root.txt": "hi"})

    StubExecutor(stubs_root=other).invoke(make_inv(run_dir, ws, "greet"))

    assert (run_dir / "from-root.txt").is_file()


def test_env_stubs_root_used_when_no_ctor_arg(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    other = tmp_path / "envroot"
    run_dir = make_run_dir(tmp_path)
    write_stub(other, "hello", "greet", {"env.txt": "hi"})
    monkeypatch.setenv("CAIRN_STUBS_ROOT", str(other))

    StubExecutor().invoke(make_inv(run_dir, ws, "greet"))

    assert (run_dir / "env.txt").is_file()


# --------------------------------------------------------------------------- #
# 6. Overlay — an existing run-dir file is overwritten.
# --------------------------------------------------------------------------- #


def test_overlay_overwrites_existing_file(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    run_dir = make_run_dir(tmp_path)
    (run_dir / "greeting.json").write_text("STALE")
    write_stub(ws / "tests/stubs", "hello", "greet", {"greeting.json": "FRESH"})

    StubExecutor().invoke(make_inv(run_dir, ws, "greet"))

    assert (run_dir / "greeting.json").read_text() == "FRESH"


# --------------------------------------------------------------------------- #
# 8. Overlay never clobbers the run's control files (defense-in-depth).
# --------------------------------------------------------------------------- #


def test_overlay_skips_run_control_files(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    run_dir = make_run_dir(tmp_path, "hello")
    (run_dir / ".cairn").mkdir()
    (run_dir / ".cairn/step-return.json").write_text("REAL-SCHEMA")
    (run_dir / "trail.jsonl").write_text("REAL-TRAIL")
    (run_dir / "gates").mkdir()
    (run_dir / "gates/tone.json").write_text("REAL-GATE")
    (run_dir / "logs/greet.log").write_text("REAL-LOG")

    # A malicious stub tree tries to overwrite every control file — plus a legit artifact.
    write_stub(ws / "tests/stubs", "hello", "greet", {
        "run.json": '{"pipeline": "evil"}',
        ".cairn/step-return.json": "POISON",
        "trail.jsonl": "POISON",
        "gates/tone.json": "POISON",
        "logs/greet.log": "POISON",
        "note.json": "{}",
    })

    result = StubExecutor().invoke(make_inv(run_dir, ws, "greet"))

    assert json.loads((run_dir / "run.json").read_text())["pipeline"] == "hello"
    assert (run_dir / ".cairn/step-return.json").read_text() == "REAL-SCHEMA"
    assert (run_dir / "trail.jsonl").read_text() == "REAL-TRAIL"
    assert (run_dir / "gates/tone.json").read_text() == "REAL-GATE"
    assert (run_dir / "logs/greet.log").read_text() == "REAL-LOG"
    assert (run_dir / "note.json").is_file()  # the real artifact still lands
    assert result.step["artifacts"] == ["note.json"]  # protected files not reported


# --------------------------------------------------------------------------- #
# 9. Dotted step id must not misfire the cycle parse.
# --------------------------------------------------------------------------- #


def test_dotted_step_id_does_not_produce_a_phantom_cycle(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    run_dir = make_run_dir(tmp_path)
    # Step id literally contains ".c1"; its log stem is 'a.c1' — must NOT read as cycle 1.
    write_stub(ws / "tests/stubs", "hello", "a.c1", {"bare.txt": "bare"})
    write_stub(ws / "tests/stubs", "hello", "a.c1.c1", {"cycled.txt": "cycled"})

    StubExecutor().invoke(make_inv(run_dir, ws, "a.c1"))

    assert (run_dir / "bare.txt").is_file()          # bare dir chosen (cycle None)
    assert not (run_dir / "cycled.txt").exists()      # phantom-cycle dir NOT chosen


# --------------------------------------------------------------------------- #
# 7. Protocol surface.
# --------------------------------------------------------------------------- #


def test_protocol_surface(tmp_path: Path) -> None:
    ex = StubExecutor()
    assert ex.name == "stub"
    assert ex.capabilities.blocking_hooks is False
    assert ex.capabilities.output_schema is False
    assert ex.capabilities.session_capture is None
    assert ex.capabilities.installs_hooks is False
    assert ex.resolve_model("reasoning", "high") == ("stub", None)
    assert ex.install_guards([], tmp_path, tmp_path) is None
    assert ex.render_workspace(tmp_path) is None
    findings = ex.doctor()
    assert findings and findings[0].level == "info"
