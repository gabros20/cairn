"""CLI wiring — subprocess + in-process exercises of the live verbs against the real hello
scaffold and the brease-ws fixture (docs/API.md §9).

Most tests drive ``cli.main(argv)`` in-process (fast, capsys-friendly); a handful use true
subprocesses where a custom ``PATH`` matters (doctor's executor probe, the guard shim exec).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import cairn
from cairn.cli import SUBCOMMANDS, main
from cairn.kernel import newkit
from cairn.kernel.types import ExitCode

REPO = Path(__file__).resolve().parents[2]
FAKEBIN = REPO / "tests" / "unit" / "fixtures" / "fakebin"
BREASE_WS = REPO / "tests" / "unit" / "fixtures" / "brease-ws"


# --------------------------------------------------------------------------- #
# Fixtures + helpers.
# --------------------------------------------------------------------------- #


@pytest.fixture
def hello_ws(tmp_path: Path) -> Path:
    """A fresh, offline-runnable workspace instantiated from the packaged scaffold."""
    return newkit.new_workspace("demo", tmp_path)


def _run_dir(ws: Path, prefix: str = "") -> Path:
    dirs = sorted(d for d in (ws / "runs").iterdir() if d.is_dir() and d.name.startswith(prefix))
    assert dirs, f"no run dir under {ws / 'runs'}"
    return dirs[-1]


def _sh(cwd: Path, *args: str, path_prepend: Path | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if path_prepend is not None:
        env["PATH"] = f"{path_prepend}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        [sys.executable, "-m", "cairn", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )


# --------------------------------------------------------------------------- #
# Frame: version + subcommand registry.
# --------------------------------------------------------------------------- #


def test_version_prints_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert cairn.__version__ in capsys.readouterr().out


def test_version_via_console_script():
    result = subprocess.run(
        [sys.executable, "-m", "cairn", "--version"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert cairn.__version__ in result.stdout


def test_all_expected_subcommands_are_registered():
    assert SUBCOMMANDS == [
        "plan", "run", "resume", "gate", "validate", "trail", "ps",
        "doctor", "test", "new", "compose", "batch", "learnings", "gc", "schedule",
    ]


def test_unknown_command_exits_config():
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == ExitCode.CONFIG


def test_no_subcommand_returns_config_and_prints_help(capsys):
    rc = main([])
    assert rc == ExitCode.CONFIG
    assert "usage" in capsys.readouterr().err.lower()


# batch/learnings/gc/schedule are wired (C6+); their behavior is exercised in the sections below.


# --------------------------------------------------------------------------- #
# plan.
# --------------------------------------------------------------------------- #


def test_plan_hello_lists_nodes(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    rc = main(["plan", "hello"])
    out = capsys.readouterr().out
    assert rc == int(ExitCode.OK)
    assert "greet" in out and "gate tone" in out and "compose" in out


def test_plan_json_shape(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    rc = main(["plan", "hello", "--json"])
    assert rc == int(ExitCode.OK)
    doc = json.loads(capsys.readouterr().out)
    assert doc["pipeline"] == "hello"
    kinds = [n["kind"] for n in doc["nodes"]]
    assert kinds == ["run", "gate", "run"]


def test_plan_json_carries_range_scoped_tool_warning(monkeypatch, capsys):
    # brease-ws declares [tools.vercel] needed_by=["deploy"]; an in-range deploy step makes
    # plan warn (offline — no check is run), and --json carries it structurally.
    monkeypatch.chdir(BREASE_WS)
    rc = main(["plan", "brease-rebuild", "--param", "url=https://acme.test", "--param", "mode=rebuild", "--json"])
    assert rc == int(ExitCode.OK)
    doc = json.loads(capsys.readouterr().out)
    assert any(
        "deploy" in w and "vercel" in w and "unverified" in w for w in doc["warnings"]
    ), doc["warnings"]


def test_plan_bad_param_exits_config_with_finding(monkeypatch, capsys):
    monkeypatch.chdir(BREASE_WS)
    rc = main(["plan", "brease-rebuild", "--param", "url=x", "--param", "mode=bogus"])
    assert rc == int(ExitCode.CONFIG)
    err = capsys.readouterr().err
    assert "mode" in err and "bogus" in err
    assert "brease-rebuild.yaml" in err  # the offending file is named


def test_plan_required_param_missing(monkeypatch, capsys):
    monkeypatch.chdir(BREASE_WS)
    rc = main(["plan", "brease-rebuild"])
    assert rc == int(ExitCode.CONFIG)
    assert "url" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# run (end-to-end offline).
# --------------------------------------------------------------------------- #


def test_run_hello_end_to_end(hello_ws, monkeypatch):
    monkeypatch.chdir(hello_ws)
    rc = main(["run", "hello", "--headless", "--gate", "tone=friendly"])
    assert rc == int(ExitCode.OK)
    rd = _run_dir(hello_ws, "hello-world")
    assert (rd / "greeting.json").is_file()
    assert (rd / "message.txt").read_text().startswith("Friendly hello, world")
    gate = json.loads((rd / "gates" / "tone.json").read_text())
    assert gate["choice"] == "friendly" and gate["by"] == "flag"


def test_run_idempotent_twice_same_dir(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    rc1 = main(["run", "hello", "--headless", "--idempotent"])
    assert rc1 == int(ExitCode.OK)
    before = sorted((hello_ws / "runs").iterdir())
    capsys.readouterr()
    rc2 = main(["run", "hello", "--headless", "--idempotent"])
    after = sorted((hello_ws / "runs").iterdir())
    assert rc2 == int(ExitCode.OK)
    assert before == after  # no new run dir minted
    assert "already done" in capsys.readouterr().out


def test_run_stub_executor_offline(monkeypatch, tmp_path, capsys):
    # A full agent pipeline "runs" offline via the stub — but brease-rebuild's agents have no
    # recorded stubs here, so the first agent step fails loudly (never a false green).
    monkeypatch.chdir(BREASE_WS)
    rc = main([
        "run", "brease-rebuild", "--executor", "stub", "--headless",
        "--param", "url=https://acme.com", "--run-dir", str(tmp_path / "r"),
    ])
    # discover has no stub dir → artifact gate fails → halt (gate-failed), not a crash.
    assert rc == int(ExitCode.GATE_FAILED)


def _twofleet_ws(tmp_path: Path) -> Path:
    """A 2-agent pipeline where `stub` is a first-class named executor and `claude` is the
    (deliberately non-runnable) default — so honoring a recorded stub override is observable:
    a resume that fell back to `claude` would try to spawn it and fail."""
    ws = newkit.new_workspace("fleet", tmp_path)
    (ws / "cairn.toml").write_text(
        (ws / "cairn.toml").read_text()
        + "\n[executors.claude]\nenabled = true\n[executors.claude.tiers]\n"
        "balanced = { model = \"sonnet\", effort = \"medium\" }\n"
        "\n[executors.stub]\nenabled = true\n[executors.stub.tiers]\nbalanced = { model = \"stub\" }\n",
        encoding="utf-8",
    )
    (ws / "agents" / "a1.yaml").write_text('description: "a1"\ntier: balanced\n', encoding="utf-8")
    (ws / "agents" / "a2.yaml").write_text('description: "a2"\ntier: balanced\n', encoding="utf-8")
    (ws / "pipelines" / "twofleet.yaml").write_text(
        "pipeline: twofleet\nversion: 1\nrun_id: \"twofleet-{date}\"\n"
        "artifacts:\n  art1: { path: art1.json, validator: validators/nonempty.py }\n"
        "  art2: { path: art2.json, validator: validators/nonempty.py }\n"
        "steps:\n  - id: s1\n    agent: a1\n    produces: [art1]\n"
        "  - id: s2\n    agent: a2\n    needs: [art1]\n    produces: [art2]\n",
        encoding="utf-8",
    )
    for step, art in (("s1", "art1"), ("s2", "art2")):
        d = ws / "tests" / "stubs" / "twofleet" / step
        d.mkdir(parents=True)
        (d / f"{art}.json").write_text('{"ok": true}\n', encoding="utf-8")
    return ws


def test_run_records_executor_overrides(tmp_path, monkeypatch):
    ws = _twofleet_ws(tmp_path)
    monkeypatch.chdir(ws)
    rc = main(["run", "twofleet", "--step-executor", "s1=stub", "--step-executor", "s2=stub", "--headless"])
    assert rc == int(ExitCode.OK)
    ex = json.loads((_run_dir(ws, "twofleet") / "run.json").read_text())["executors"]
    assert ex["default"] == "claude"
    assert ex["overrides"] == {"s1": "stub", "s2": "stub"}


def test_resume_honors_recorded_executor_overrides(tmp_path, monkeypatch):
    ws = _twofleet_ws(tmp_path)
    monkeypatch.chdir(ws)
    assert main(["run", "twofleet", "--step-executor", "s1=stub", "--step-executor", "s2=stub", "--headless"]) == 0
    rd = _run_dir(ws, "twofleet")
    (rd / "art2.json").unlink()  # force s2 to re-run on resume
    # If resume fell back to the default (claude), spawning it would fail; a clean 0 proves the
    # recorded stub override was honored.
    assert main(["resume", str(rd)]) == int(ExitCode.OK)
    assert (rd / "art2.json").is_file()


def _phantom_ws(tmp_path: Path) -> Path:
    """A workspace whose cairn.toml defines an executor `phantom` with NO registered plugin —
    it plans fine (it has a config table) but can't be built."""
    ws = newkit.new_workspace("ph", tmp_path)
    (ws / "cairn.toml").write_text(
        (ws / "cairn.toml").read_text()
        + "\n[executors.phantom]\nenabled = true\n[executors.phantom.tiers]\nbalanced = { model = \"ghost\" }\n",
        encoding="utf-8",
    )
    (ws / "agents" / "a1.yaml").write_text('description: "a1"\ntier: balanced\n', encoding="utf-8")
    (ws / "pipelines" / "ph.yaml").write_text(
        "pipeline: ph\nversion: 1\nrun_id: \"ph-{date}\"\n"
        "artifacts:\n  a1: { path: a1.json, validator: validators/nonempty.py }\n"
        "steps:\n  - id: s1\n    agent: a1\n    produces: [a1]\n",
        encoding="utf-8",
    )
    return ws


def test_run_missing_executor_plugin_is_typed_error(tmp_path, monkeypatch, capsys):
    ws = _phantom_ws(tmp_path)
    monkeypatch.chdir(ws)
    rc = main(["run", "ph", "--step-executor", "s1=phantom", "--headless"])
    assert rc == int(ExitCode.EXECUTOR)
    assert "no such executor plugin" in capsys.readouterr().err


def test_resume_missing_executor_plugin_is_typed_error(tmp_path, monkeypatch, capsys):
    ws = _phantom_ws(tmp_path)
    monkeypatch.chdir(ws)
    main(["run", "ph", "--step-executor", "s1=phantom", "--headless"])  # records the override, halts 4
    rd = _run_dir(ws, "ph")
    capsys.readouterr()
    rc = main(["resume", str(rd)])  # reconstructs phantom from run.json → same typed error
    assert rc == int(ExitCode.EXECUTOR)
    assert "no such executor plugin" in capsys.readouterr().err


def test_resume_after_pipeline_file_deleted_is_clean_config_error(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    assert main(["run", "hello", "--headless"]) == 0
    rd = _run_dir(hello_ws, "hello-world")
    (hello_ws / "pipelines" / "hello.yaml").unlink()
    rc = main(["resume", str(rd)])
    err = capsys.readouterr().err
    assert rc == int(ExitCode.CONFIG)
    assert "no longer exists" in err and "hello.yaml" in err


# --------------------------------------------------------------------------- #
# resume: cross-version gate (DISTRIBUTION §3, Run-dir format).
# --------------------------------------------------------------------------- #


def _set_run_version(rd: Path, version: str) -> None:
    p = rd / "run.json"
    doc = json.loads(p.read_text())
    doc["cairn_version"] = version
    p.write_text(json.dumps(doc, indent=2))


def test_resume_refuses_cross_major_version_without_force(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    assert main(["run", "hello", "--headless", "--gate", "tone=friendly"]) == 0
    rd = _run_dir(hello_ws, "hello-world")
    _set_run_version(rd, "9.0.0")
    rc = main(["resume", str(rd)])
    err = capsys.readouterr().err
    assert rc == int(ExitCode.CONFIG)
    assert "9.0.0" in err and cairn.__version__ in err  # names both versions
    assert f"resume {rd} --force" in err  # the remedy names the flag-bearing command


def test_resume_cross_major_version_bypassed_by_force(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    assert main(["run", "hello", "--headless", "--gate", "tone=friendly"]) == 0
    rd = _run_dir(hello_ws, "hello-world")
    (rd / "message.txt").unlink()  # force a genuine re-run, not a done-noop
    _set_run_version(rd, "9.0.0")
    rc = main(["resume", str(rd), "--force"])
    err = capsys.readouterr().err
    assert rc == int(ExitCode.OK)
    assert "--force" in err  # the override warning fired
    assert (rd / "message.txt").exists()


def test_resume_cross_minor_version_warns_and_proceeds(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    assert main(["run", "hello", "--headless", "--gate", "tone=friendly"]) == 0
    rd = _run_dir(hello_ws, "hello-world")
    (rd / "message.txt").unlink()
    _set_run_version(rd, "0.9.0")
    rc = main(["resume", str(rd)])
    err = capsys.readouterr().err
    assert rc == int(ExitCode.OK)  # a cross-minor drift never refuses
    assert "0.9.0" in err and "version drift" in err
    assert (rd / "message.txt").exists()


def test_run_run_dir_existing_refuses_pipeline_drift(hello_ws, monkeypatch, capsys):
    # `run --run-dir <existing>` (no --idempotent) resumes — it must pass the same drift
    # guard as `cairn resume`, not silently resume the old run against an edited file.
    monkeypatch.chdir(hello_ws)
    assert main(["run", "hello", "--headless", "--gate", "tone=friendly"]) == 0
    rd = _run_dir(hello_ws, "hello-world")
    pfile = hello_ws / "pipelines" / "hello.yaml"
    pfile.write_text(pfile.read_text() + "\n# drift\n", encoding="utf-8")
    rc = main(["run", "hello", "--headless", "--run-dir", str(rd)])
    err = capsys.readouterr().err
    assert rc == int(ExitCode.CONFIG)
    assert "hash drift" in err
    assert f"resume {rd} --force" in err  # the remedy names the flag-bearing command


def test_run_run_dir_existing_refuses_cross_major_version(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    assert main(["run", "hello", "--headless", "--gate", "tone=friendly"]) == 0
    rd = _run_dir(hello_ws, "hello-world")
    _set_run_version(rd, "9.0.0")
    rc = main(["run", "hello", "--headless", "--run-dir", str(rd)])
    err = capsys.readouterr().err
    assert rc == int(ExitCode.CONFIG)
    assert "9.0.0" in err and cairn.__version__ in err  # names both versions
    assert f"resume {rd} --force" in err


def test_run_run_dir_existing_unreadable_run_json_is_clean_config_error(hello_ws, monkeypatch, capsys):
    # A dir explicitly chosen for resume whose manifest can't be read is a config error —
    # fail loud like `cairn resume`, never degrade to a misleading "no cairn version"
    # warning followed by an uncaught traceback in the walk.
    monkeypatch.chdir(hello_ws)
    assert main(["run", "hello", "--headless", "--gate", "tone=friendly"]) == 0
    rd = _run_dir(hello_ws, "hello-world")
    (rd / "run.json").write_text("{corrupt", encoding="utf-8")
    rc = main(["run", "hello", "--headless", "--run-dir", str(rd)])
    err = capsys.readouterr().err
    assert rc == int(ExitCode.CONFIG)
    assert "cannot read" in err and "run.json" in err
    assert "records no cairn version" not in err  # no misleading version warning


def test_run_run_dir_existing_clean_path_resumes(hello_ws, monkeypatch):
    # Same pipeline, same version → the guards stay silent and the resume completes.
    monkeypatch.chdir(hello_ws)
    assert main(["run", "hello", "--headless", "--gate", "tone=friendly"]) == 0
    rd = _run_dir(hello_ws, "hello-world")
    (rd / "message.txt").unlink()
    rc = main(["run", "hello", "--headless", "--run-dir", str(rd)])
    assert rc == int(ExitCode.OK)
    assert (rd / "message.txt").exists()


def test_resume_same_version_is_silent(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    assert main(["run", "hello", "--headless", "--gate", "tone=friendly"]) == 0
    rd = _run_dir(hello_ws, "hello-world")
    (rd / "message.txt").unlink()  # a real resume — the run wrote the installed version
    rc = main(["resume", str(rd)])
    err = capsys.readouterr().err
    assert rc == int(ExitCode.OK)
    assert "version drift" not in err  # no version noise on a same-version resume


# --------------------------------------------------------------------------- #
# resume + gate verb.
# --------------------------------------------------------------------------- #


def test_resume_after_deleting_an_artifact(hello_ws, monkeypatch):
    monkeypatch.chdir(hello_ws)
    assert main(["run", "hello", "--headless", "--gate", "tone=friendly"]) == 0
    rd = _run_dir(hello_ws, "hello-world")
    (rd / "message.txt").unlink()
    rc = main(["resume", str(rd)])
    assert rc == int(ExitCode.OK)
    assert (rd / "message.txt").read_text().startswith("Friendly hello")


def test_gate_verb_then_resume_completes(hello_ws, monkeypatch):
    monkeypatch.chdir(hello_ws)
    # Run only up to `greet`, so the tone gate is still unanswered on disk.
    assert main(["run", "hello", "--to", "greet", "--headless"]) == 0
    rd = _run_dir(hello_ws, "hello-world")
    assert not (rd / "gates" / "tone.json").exists()

    assert main(["gate", str(rd), "tone=formal"]) == int(ExitCode.OK)
    decision = json.loads((rd / "gates" / "tone.json").read_text())
    assert decision["choice"] == "formal" and decision["by"] == "external"

    assert main(["resume", str(rd)]) == int(ExitCode.OK)
    assert (rd / "message.txt").read_text().startswith("Formal hello")


def test_manual_step_needs_human_then_resume(hello_ws, monkeypatch):
    monkeypatch.chdir(hello_ws)
    (hello_ws / "pipelines" / "manualflow.yaml").write_text(
        "pipeline: manualflow\nversion: 1\nrun_id: \"manualflow-{date}\"\n"
        "artifacts:\n  token: { path: token.txt, validator: validators/nonempty.py }\n"
        "steps:\n"
        "  - id: approve\n    manual: \"write token.txt\"\n    produces: [token]\n"
        "  - id: after\n    run: \"echo done\"\n    needs: [token]\n",
        encoding="utf-8",
    )
    rc = main(["run", "manualflow", "--headless"])
    assert rc == int(ExitCode.NEEDS_HUMAN)
    rd = _run_dir(hello_ws, "manualflow")
    (rd / "token.txt").write_text("ok\n", encoding="utf-8")
    assert main(["resume", str(rd)]) == int(ExitCode.OK)


def test_gate_verb_bad_assignment(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    assert main(["run", "hello", "--to", "greet", "--headless"]) == 0
    rd = _run_dir(hello_ws, "hello-world")
    rc = main(["gate", str(rd), "no-equals-sign"])
    assert rc == int(ExitCode.CONFIG)
    assert "<name>=<choice>" in capsys.readouterr().err


def test_gate_rejects_non_run_dir(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = main(["gate", str(empty), "tone=friendly"])
    assert rc == int(ExitCode.CONFIG)
    assert "not a run dir" in capsys.readouterr().err


def test_gate_rejects_unknown_gate_name(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    assert main(["run", "hello", "--to", "greet", "--headless"]) == 0
    rd = _run_dir(hello_ws, "hello-world")
    rc = main(["gate", str(rd), "nope=friendly"])
    err = capsys.readouterr().err
    assert rc == int(ExitCode.CONFIG)
    assert "no gate 'nope'" in err and "tone" in err  # names the real gate


def test_gate_rejects_choice_not_an_option(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    assert main(["run", "hello", "--to", "greet", "--headless"]) == 0
    rd = _run_dir(hello_ws, "hello-world")
    rc = main(["gate", str(rd), "tone=loud"])
    err = capsys.readouterr().err
    assert rc == int(ExitCode.CONFIG)
    assert "not an option" in err and "friendly" in err and "formal" in err


def test_gate_refuses_to_overwrite_answered_gate(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    assert main(["run", "hello", "--headless"]) == 0  # tone resolves to default 'friendly'
    rd = _run_dir(hello_ws, "hello-world")
    rc = main(["gate", str(rd), "tone=formal"])
    err = capsys.readouterr().err
    assert rc == int(ExitCode.CONFIG)
    assert "already answered" in err and "friendly" in err  # names the recorded choice


# --------------------------------------------------------------------------- #
# validate.
# --------------------------------------------------------------------------- #


def test_validate_happy_and_broken(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    assert main(["run", "hello", "--headless", "--gate", "tone=friendly"]) == 0
    rd = _run_dir(hello_ws, "hello-world")

    capsys.readouterr()
    assert main(["validate", str(rd)]) == int(ExitCode.OK)
    assert "greeting" in capsys.readouterr().out

    (rd / "message.txt").write_bytes(b"")  # truly empty → nonempty.py rejects
    capsys.readouterr()
    rc = main(["validate", str(rd), "message"])
    out = capsys.readouterr().out
    assert rc == int(ExitCode.GATE_FAILED)
    assert "empty" in out


# --------------------------------------------------------------------------- #
# trail + ps.
# --------------------------------------------------------------------------- #


def test_trail_follow_json_streams_and_terminates(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    assert main(["run", "hello", "--headless"]) == 0
    rd = _run_dir(hello_ws, "hello-world")

    capsys.readouterr()  # drop the run's own stdout
    rc = main(["trail", str(rd), "--follow", "--json", "--since", "0"])
    assert rc == int(ExitCode.OK)
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert lines[0]["event"] == "run-start"
    assert lines[-1]["event"] == "run-done"
    assert [ev["seq"] for ev in lines] == sorted(ev["seq"] for ev in lines)


def test_trail_since_filters(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    assert main(["run", "hello", "--headless"]) == 0
    rd = _run_dir(hello_ws, "hello-world")
    capsys.readouterr()  # drop the run's own stdout
    main(["trail", str(rd), "--follow", "--json", "--since", "3"])
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert all(ev["seq"] > 3 for ev in lines)


def test_ps_table_on_two_runs(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    main(["run", "hello", "--headless"])
    main(["run", "hello", "--param", "name=Ada", "--headless"])
    capsys.readouterr()
    assert main(["ps"]) == int(ExitCode.OK)
    out = capsys.readouterr().out
    assert "hello-world" in out and "hello-Ada" in out and "done" in out


def test_ps_json(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    main(["run", "hello", "--headless"])
    capsys.readouterr()
    assert main(["ps", "--json"]) == int(ExitCode.OK)
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]["status"] == "done"


# --------------------------------------------------------------------------- #
# doctor.
# --------------------------------------------------------------------------- #


def test_doctor_hello_green(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{os.environ['PATH']}")
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == int(ExitCode.OK)
    assert "executor claude" in out
    assert "plan green" in out


def test_doctor_skips_param_required_pipeline_with_note(monkeypatch, capsys):
    # brease-rebuild requires `url` with no default: doctor must note it, not fail the lint.
    monkeypatch.chdir(BREASE_WS)
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{os.environ['PATH']}")
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == int(ExitCode.OK)
    assert "requires params: url" in out
    assert "plan green" in out  # the workspace is still green overall


def test_doctor_genuine_config_error_still_fails(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{os.environ['PATH']}")
    (hello_ws / "pipelines" / "broken.yaml").write_text(
        "pipeline: broken\nversion: 1\nrun_id: \"broken-{date}\"\n"
        "steps:\n  - id: x\n    run: \"echo hi\"\n    needs: [ghost]\n",  # ghost is never produced
        encoding="utf-8",
    )
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == int(ExitCode.CONFIG)
    assert "broken" in out


def test_doctor_missing_tool_prints_hint(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{os.environ['PATH']}")
    toml = hello_ws / "cairn.toml"
    toml.write_text(
        toml.read_text() + '\n[tools.badtool]\ncheck = "false"\ninstall = "brew install badtool"\n',
        encoding="utf-8",
    )
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == int(ExitCode.OK)  # a scoped tool failure warns, never fails the exit
    assert "brew install badtool" in out


def test_doctor_reports_requires_satisfied(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{os.environ['PATH']}")
    toml = hello_ws / "cairn.toml"
    # A top-level bare key must precede any table header in TOML — prepend it.
    toml.write_text('requires = ">=0.1,<0.2"\n' + toml.read_text(), encoding="utf-8")
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == int(ExitCode.OK)
    assert 'requires ">=0.1,<0.2"' in out
    assert f"satisfied by {cairn.__version__}" in out


def test_doctor_reports_requires_not_satisfied_and_fails(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{os.environ['PATH']}")
    toml = hello_ws / "cairn.toml"
    toml.write_text('requires = ">=9.0"\n' + toml.read_text(), encoding="utf-8")
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == int(ExitCode.CONFIG)  # an unsatisfied pin is a doctor error
    assert 'requires ">=9.0"' in out and "NOT satisfied by" in out
    assert "uv tool install" in out  # the fix hint


def test_doctor_no_requires_pin_prints_no_requires_line(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    monkeypatch.setenv("PATH", f"{FAKEBIN}{os.pathsep}{os.environ['PATH']}")
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == int(ExitCode.OK)
    assert 'requires "' not in out  # no pin declared ⇒ no requires line


# --------------------------------------------------------------------------- #
# test (binds testkit).
# --------------------------------------------------------------------------- #


def test_test_binds_pipeline_suite_green(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    (hello_ws / "tests").mkdir(exist_ok=True)
    (hello_ws / "tests" / "matrix.yaml").write_text(
        "hello:\n  - { name: world, _gates: { tone: friendly } }\n", encoding="utf-8"
    )
    rc = main(["test", "pipelines"])
    out = capsys.readouterr().out
    assert rc == int(ExitCode.OK)
    assert "pipelines: 1 passed, 0 failed" in out


def test_test_surfaces_suite_notes(hello_ws, monkeypatch, capsys):
    # A day-0 workspace with no fixtures still surfaces the "(no fixtures)" coverage signal —
    # not just bare passed/failed counts.
    monkeypatch.chdir(hello_ws)
    rc = main(["test"])
    out = capsys.readouterr().out
    assert rc == int(ExitCode.OK)
    assert out.count("(no fixtures)") == 4  # one per suite


def test_test_envelopes_update_is_reachable(hello_ws, monkeypatch, capsys):
    # `--update` must be wired so goldens can be seeded on day 0.
    monkeypatch.chdir(hello_ws)
    rc = main(["test", "envelopes", "--update"])
    assert rc == int(ExitCode.OK)


def test_test_exits_nonzero_and_shows_failures(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    fx = hello_ws / "tests" / "fixtures" / "greeting"
    fx.mkdir(parents=True)
    (fx / "valid-broken.json").write_text("not json at all", encoding="utf-8")  # a valid-* must pass
    rc = main(["test", "validators"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL:" in out


def test_test_pipeline_flag_removed(hello_ws, monkeypatch):
    # The silent no-op `--pipeline` filter was dropped; argparse now rejects it.
    monkeypatch.chdir(hello_ws)
    with pytest.raises(SystemExit) as exc:
        main(["test", "validators", "--pipeline", "hello"])
    assert exc.value.code == 2


def test_test_record_harvests_run(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    main(["run", "hello", "--headless"])
    rd = _run_dir(hello_ws, "hello-world")
    capsys.readouterr()
    rc = main(["test", "record", str(rd)])
    assert rc == int(ExitCode.OK)
    assert (hello_ws / "tests" / "stubs" / "hello" / "greet" / "greeting.json").is_file()


# --------------------------------------------------------------------------- #
# compose.
# --------------------------------------------------------------------------- #


def test_compose_prints_six_block_envelope(monkeypatch, capsys):
    monkeypatch.chdir(BREASE_WS)
    rc = main(["compose", "brease-rebuild", "discover", "--param", "url=https://acme.com"])
    out = capsys.readouterr().out
    assert rc == int(ExitCode.OK)
    for header in ("# MISSION", "# CONTRACT", "# SKILLS", "# TRAIL", "# DOCTRINE", "# RETURN"):
        assert header in out


def test_compose_rejects_non_agent_step(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    rc = main(["compose", "hello", "greet"])
    assert rc == int(ExitCode.CONFIG)
    assert "agent" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# new.
# --------------------------------------------------------------------------- #


def test_new_workspace_instantiates_and_plans_green(tmp_path, monkeypatch, capsys):
    rc = main(["new", "workspace", "fresh", "--dir", str(tmp_path)])
    assert rc == int(ExitCode.OK)
    ws = tmp_path / "fresh"
    assert (ws / "cairn.toml").is_file()
    assert "{{WORKSPACE_NAME}}" not in (ws / "cairn.toml").read_text()
    monkeypatch.chdir(ws)
    capsys.readouterr()
    assert main(["plan", "hello"]) == int(ExitCode.OK)


def test_new_stub_pipeline_plans(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    assert main(["new", "pipeline", "mypipe"]) == int(ExitCode.OK)
    capsys.readouterr()
    assert main(["plan", "mypipe"]) == int(ExitCode.OK)


# --------------------------------------------------------------------------- #
# guard shim wiring (true subprocess — a custom PATH must reach the exec).
# --------------------------------------------------------------------------- #


def _guard_ws(tmp_path: Path) -> tuple[Path, Path]:
    ws = tmp_path / "gws"
    (ws / "pipelines").mkdir(parents=True)
    (ws / "guards").mkdir()
    bindir = ws / "bin"
    bindir.mkdir()
    (bindir / "faketool").write_text("#!/bin/sh\necho \"faketool ran: $*\"\nexit 0\n", encoding="utf-8")
    (bindir / "faketool").chmod(0o755)
    (ws / "guards" / "deny.py").write_text(
        "import sys\nprint('denied: faketool danger blocked', file=sys.stderr)\nsys.exit(2)\n",
        encoding="utf-8",
    )
    (ws / "cairn.toml").write_text(
        '[workspace]\nname = "gws"\nruns_dir = "runs"\ndefault_executor = "claude"\n',
        encoding="utf-8",
    )
    guard = (
        "guards:\n  - name: no-danger\n    match: { tool: bash, command: \"faketool danger*\" }\n"
        "    check: guards/deny.py\n    enforce: [shim]\n    on_error: deny\n"
    )
    (ws / "pipelines" / "denied.yaml").write_text(
        f"pipeline: denied\nversion: 1\nrun_id: \"denied-{{date}}\"\n{guard}"
        "steps:\n  - id: danger-cmd\n    run: \"faketool danger now\"\n",
        encoding="utf-8",
    )
    (ws / "pipelines" / "allowed.yaml").write_text(
        f"pipeline: allowed\nversion: 1\nrun_id: \"allowed-{{date}}\"\n{guard}"
        "steps:\n  - id: safe-cmd\n    run: \"faketool safe now\"\n",
        encoding="utf-8",
    )
    return ws, bindir


def test_guard_shim_denies_matching_command(tmp_path):
    ws, bindir = _guard_ws(tmp_path)
    res = _sh(ws, "run", "denied", "--headless", path_prepend=bindir)
    assert res.returncode == int(ExitCode.GATE_FAILED)
    rd = _run_dir(ws, "denied")
    log = (rd / "logs" / "danger-cmd.log").read_text()
    assert "denied: faketool danger blocked" in log


def test_guard_shim_allows_nonmatching_command(tmp_path):
    ws, bindir = _guard_ws(tmp_path)
    res = _sh(ws, "run", "allowed", "--headless", path_prepend=bindir)
    assert res.returncode == int(ExitCode.OK)
    rd = _run_dir(ws, "allowed")
    log = (rd / "logs" / "safe-cmd.log").read_text()
    assert "faketool ran: safe now" in log  # the real binary executed through the shim


# --------------------------------------------------------------------------- #
# Wheel packaging: `cairn new workspace` must work from an installed copy (the wheel
# force-includes templates/workspace → cairn/_templates/workspace). True install smoke.
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv required to build + install the wheel")
def test_wheel_ships_templates_and_new_workspace_works_installed(tmp_path):
    dist = tmp_path / "dist"
    build = subprocess.run(
        ["uv", "build", "--wheel", "-o", str(dist)], cwd=str(REPO), capture_output=True, text=True
    )
    assert build.returncode == 0, build.stderr
    wheels = list(dist.glob("*.whl"))
    assert wheels, "no wheel built"

    venv = tmp_path / "venv"
    assert subprocess.run(["uv", "venv", str(venv)], capture_output=True, text=True).returncode == 0
    py = venv / "bin" / "python"
    install = subprocess.run(
        ["uv", "pip", "install", "--python", str(py), str(wheels[0])], capture_output=True, text=True
    )
    assert install.returncode == 0, install.stderr

    cairn_bin = venv / "bin" / "cairn"
    work = tmp_path / "work"
    work.mkdir()
    made = subprocess.run([str(cairn_bin), "new", "workspace", "x"], cwd=str(work), capture_output=True, text=True)
    assert made.returncode == 0, made.stderr
    assert (work / "x" / "pipelines" / "hello.yaml").is_file()  # templates shipped in the wheel
    planned = subprocess.run([str(cairn_bin), "plan", "hello"], cwd=str(work / "x"), capture_output=True, text=True)
    assert planned.returncode == 0, planned.stderr
    assert "hello" in planned.stdout


# --------------------------------------------------------------------------- #
# batch (C6+ wiring — real `python -m cairn run` children on the hello scaffold).
# --------------------------------------------------------------------------- #


def test_batch_runs_each_line_and_reports(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    pf = hello_ws / "sites.jsonl"
    pf.write_text('{"name": "Ada"}\n{"name": "Bob"}\n', encoding="utf-8")
    rc = main(["batch", "hello", "--params-file", str(pf), "-j", "2"])
    out = capsys.readouterr().out
    assert rc == int(ExitCode.OK)
    dirs = [d for d in (hello_ws / "runs").iterdir() if d.is_dir()]
    assert len(dirs) == 2  # one child run dir per JSONL line
    assert "2 run(s)" in out and "0 failed" in out


def test_batch_bad_params_file_is_config_error(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    pf = hello_ws / "bad.jsonl"
    pf.write_text("this is not json\n", encoding="utf-8")
    rc = main(["batch", "hello", "--params-file", str(pf)])
    assert rc == int(ExitCode.CONFIG)
    assert "not valid JSON" in capsys.readouterr().err


def test_batch_aggregates_child_failure_exit(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    pf = hello_ws / "s.jsonl"
    pf.write_text('{"name": "Ada"}\n', encoding="utf-8")
    # A child `cairn run nope-pipeline` exits CONFIG(2); batch surfaces the worst child code.
    rc = main(["batch", "nope-pipeline", "--params-file", str(pf)])
    err = capsys.readouterr()
    assert rc == int(ExitCode.CONFIG)
    assert "1 failed" in err.out
    # the failed run's stderr tail is surfaced, indented, under its summary line
    assert "✗ [0]" in err.err
    assert "nope-pipeline" in err.err  # the child's config-error reason bubbles up


def test_batch_summary_renders_failed_stderr_tail(hello_ws, monkeypatch, capsys):
    # The batch summary prints the failed child's stderr tail as an indented block; a long
    # tail is truncated with a pointer at the run dir's logs.
    from cairn.kernel.batchkit import BatchResult, RunOutcome

    monkeypatch.chdir(hello_ws)
    pf = hello_ws / "s.jsonl"
    pf.write_text('{"name": "Ada"}\n', encoding="utf-8")

    long_tail = "\n".join(f"reason line {i}" for i in range(12))
    fake = BatchResult(
        pipeline="hello",
        outcomes=(
            RunOutcome(
                index=0,
                params={"name": "Ada"},
                run_dir=Path("/runs/ada"),
                exit_code=6,
                duration_s=0.1,
                error=long_tail,
            ),
        ),
        exit_code=6,
    )
    monkeypatch.setattr("cairn.cli.run_batch", lambda *a, **k: fake)
    rc = main(["batch", "hello", "--params-file", str(pf)])
    err = capsys.readouterr().err
    assert rc == 6
    assert "✗ [0] ada  exit 6" in err
    assert "      reason line 0" in err  # indented tail block
    assert "+6 more line(s)" in err and "/runs/ada" in err  # truncation + pointer


# --------------------------------------------------------------------------- #
# run --idempotent reconciliation (delegates to schedkit.find_idempotent_run).
# --------------------------------------------------------------------------- #


def test_run_idempotent_resumes_incomplete_equivalent_run(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    # A manual step halts the first run NEEDS_HUMAN → a genuinely incomplete (status != done) dir.
    (hello_ws / "pipelines" / "manualflow.yaml").write_text(
        "pipeline: manualflow\nversion: 1\nrun_id: \"manualflow-{date}\"\n"
        "artifacts:\n  token: { path: token.txt, validator: validators/nonempty.py }\n"
        "steps:\n"
        "  - id: approve\n    manual: \"write token.txt\"\n    produces: [token]\n"
        "  - id: after\n    run: \"echo done\"\n    needs: [token]\n",
        encoding="utf-8",
    )
    assert main(["run", "manualflow", "--headless", "--idempotent"]) == int(ExitCode.NEEDS_HUMAN)
    before = sorted((hello_ws / "runs").iterdir())
    assert len(before) == 1
    rd = before[0]
    (rd / "token.txt").write_text("ok\n", encoding="utf-8")  # satisfy the manual step

    capsys.readouterr()
    # A fresh --idempotent firing must RESUME that same dir (same date/params key), not mint one.
    rc = main(["run", "manualflow", "--headless", "--idempotent"])
    assert rc == int(ExitCode.OK)
    after = sorted((hello_ws / "runs").iterdir())
    assert after == before  # resumed in place, no variant created


def test_run_idempotent_incomplete_match_refuses_pipeline_drift(hello_ws, monkeypatch, capsys):
    # A timer re-fire after a pipeline edit must fail loud (the same drift guard `cairn
    # resume` enforces), never silently resume the old run against the new file.
    monkeypatch.chdir(hello_ws)
    pfile = hello_ws / "pipelines" / "manualflow.yaml"
    pfile.write_text(
        "pipeline: manualflow\nversion: 1\nrun_id: \"manualflow-{date}\"\n"
        "artifacts:\n  token: { path: token.txt, validator: validators/nonempty.py }\n"
        "steps:\n"
        "  - id: approve\n    manual: \"write token.txt\"\n    produces: [token]\n"
        "  - id: after\n    run: \"echo done\"\n    needs: [token]\n",
        encoding="utf-8",
    )
    assert main(["run", "manualflow", "--headless", "--idempotent"]) == int(ExitCode.NEEDS_HUMAN)
    before = sorted((hello_ws / "runs").iterdir())

    pfile.write_text(pfile.read_text() + "# edited since the run was planned\n", encoding="utf-8")
    capsys.readouterr()
    rc = main(["run", "manualflow", "--headless", "--idempotent"])
    err = capsys.readouterr().err
    assert rc == int(ExitCode.CONFIG)
    assert "hash drift" in err
    # The remedy must name the command that actually takes --force (`cairn run` has no
    # --force flag), pointing at the drifted run dir.
    assert f"cairn resume {before[0]} --force" in err
    assert sorted((hello_ws / "runs").iterdir()) == before  # nothing resumed, nothing minted


_MANUALFLOW = (
    "pipeline: manualflow\nversion: 1\nrun_id: \"manualflow-{date}\"\n"
    "artifacts:\n  token: { path: token.txt, validator: validators/nonempty.py }\n"
    "steps:\n"
    "  - id: approve\n    manual: \"write token.txt\"\n    produces: [token]\n"
    "  - id: after\n    run: \"echo done\"\n    needs: [token]\n"
)


def test_run_idempotent_incomplete_match_refuses_cross_major_version(hello_ws, monkeypatch, capsys):
    # A timer re-fire on a run dir minted by a different cairn MAJOR must refuse like
    # `cairn resume` does — `cairn run` has no --force, so the remedy names resume.
    monkeypatch.chdir(hello_ws)
    (hello_ws / "pipelines" / "manualflow.yaml").write_text(_MANUALFLOW, encoding="utf-8")
    assert main(["run", "manualflow", "--headless", "--idempotent"]) == int(ExitCode.NEEDS_HUMAN)
    before = sorted((hello_ws / "runs").iterdir())
    _set_run_version(before[0], "9.0.0")

    capsys.readouterr()
    rc = main(["run", "manualflow", "--headless", "--idempotent"])
    err = capsys.readouterr().err
    assert rc == int(ExitCode.CONFIG)
    assert "9.0.0" in err and cairn.__version__ in err  # names both versions
    assert f"cairn resume {before[0]} --force" in err
    assert sorted((hello_ws / "runs").iterdir()) == before  # nothing resumed, nothing minted


def test_run_idempotent_match_with_invalid_run_json_is_clean_config_error(hello_ws, monkeypatch, capsys):
    # A key-matching run.json that fails the cairn:run schema (valid JSON, so schedkit still
    # matches it) must fail loud at the guard — exit CONFIG with the read error, not warn
    # "records no cairn version" and then blow up (exit 4) inside the walk.
    monkeypatch.chdir(hello_ws)
    (hello_ws / "pipelines" / "manualflow.yaml").write_text(_MANUALFLOW, encoding="utf-8")
    assert main(["run", "manualflow", "--headless", "--idempotent"]) == int(ExitCode.NEEDS_HUMAN)
    before = sorted((hello_ws / "runs").iterdir())
    rd = before[0]
    doc = json.loads((rd / "run.json").read_text())
    del doc["cairn_version"]  # schema-required — load_run refuses, idempotency key still matches
    (rd / "run.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")

    capsys.readouterr()
    rc = main(["run", "manualflow", "--headless", "--idempotent"])
    err = capsys.readouterr().err
    assert rc == int(ExitCode.CONFIG)
    assert "cannot read" in err and "run.json" in err
    assert "records no cairn version" not in err  # no misleading version warning
    assert sorted((hello_ws / "runs").iterdir()) == before  # nothing resumed, nothing minted


# --------------------------------------------------------------------------- #
# learnings.
# --------------------------------------------------------------------------- #


def test_learnings_empty_runs_root(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    main(["run", "hello", "--headless"])  # a shell-only run emits no learn events
    capsys.readouterr()
    rc = main(["learnings"])
    assert rc == int(ExitCode.OK)
    assert "no learnings found" in capsys.readouterr().out


def test_learnings_aggregates_and_filters_by_tag(hello_ws, monkeypatch, capsys):
    from cairn.kernel.trail import TrailWriter

    monkeypatch.chdir(hello_ws)
    rd = hello_ws / "runs" / "hello-world-20260703"
    rd.mkdir(parents=True)
    (rd / "run.json").write_text('{"pipeline": "hello"}', encoding="utf-8")
    w = TrailWriter(rd, "hello-world-20260703")
    w.emit("run-start")
    w.emit("learn", node="greet", data={"note": "keep it short", "tag": "copy"})
    w.emit("learn", node="greet", data={"note": "sites never idle", "tag": "crawl"})
    w.close()

    capsys.readouterr()
    assert main(["learnings"]) == int(ExitCode.OK)
    out = capsys.readouterr().out
    assert "keep it short" in out and "sites never idle" in out

    capsys.readouterr()
    assert main(["learnings", "--tag", "copy"]) == int(ExitCode.OK)
    out = capsys.readouterr().out
    assert "keep it short" in out and "sites never idle" not in out


def test_learnings_bad_since_is_config_error(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    rc = main(["learnings", "--since", "not-a-date"])
    assert rc == int(ExitCode.CONFIG)
    assert "invalid --since" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# gc (dry-run by default; --apply deletes).
# --------------------------------------------------------------------------- #


def _make_gc_run(runs_root: Path, run_id: str, *, created_at: str, pipeline: str = "hello") -> Path:
    from cairn.kernel.trail import TrailWriter

    rd = runs_root / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(
        json.dumps({"run_id": run_id, "pipeline": pipeline, "created_at": created_at, "status": "done"}),
        encoding="utf-8",
    )
    w = TrailWriter(rd, run_id)
    w.emit("run-start")
    w.emit("run-done")
    w.close()
    (rd / "artifacts").mkdir(exist_ok=True)
    (rd / "artifacts" / "big.json").write_text("x" * 2048, encoding="utf-8")
    return rd


def test_gc_no_rule_is_config_error(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    rc = main(["gc"])
    assert rc == int(ExitCode.CONFIG)
    assert "at least one" in capsys.readouterr().err


def test_gc_dry_run_lists_and_deletes_nothing(hello_ws, monkeypatch, capsys):
    from datetime import datetime, timezone

    monkeypatch.chdir(hello_ws)
    runs = hello_ws / "runs"
    old = _make_gc_run(runs, "hello-old-20260101", created_at="2026-01-01T00:00:00Z")
    fresh = _make_gc_run(runs, "hello-fresh", created_at=datetime.now(timezone.utc).isoformat())

    capsys.readouterr()
    rc = main(["gc", "--keep-days", "7"])
    out = capsys.readouterr().out
    assert rc == int(ExitCode.OK)
    assert "dry-run" in out and "hello-old-20260101" in out
    assert old.exists() and fresh.exists()  # dry-run deletes nothing


def test_gc_apply_deletes_selected_run(hello_ws, monkeypatch, capsys):
    from datetime import datetime, timezone

    monkeypatch.chdir(hello_ws)
    runs = hello_ws / "runs"
    old = _make_gc_run(runs, "hello-old-20260101", created_at="2026-01-01T00:00:00Z")
    fresh = _make_gc_run(runs, "hello-fresh", created_at=datetime.now(timezone.utc).isoformat())

    capsys.readouterr()
    rc = main(["gc", "--keep-days", "7", "--apply"])
    out = capsys.readouterr().out
    assert rc == int(ExitCode.OK)
    assert "deleted 1 run(s)" in out
    assert not old.exists() and fresh.exists()


# --------------------------------------------------------------------------- #
# schedule — a FAKE runner + tmp target dirs; NEVER the real crontab/launchctl/systemctl.
# --------------------------------------------------------------------------- #


class _FakeRunner:
    """Records every host invocation and returns canned results — the schedkit effect seam,
    substituted so no test touches the real crontab / launchctl / systemctl."""

    def __init__(self, returncode: int = 0, crontab: str = "", stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.calls: list[dict] = []
        self._crontab = crontab
        self._stdout = stdout
        self._stderr = stderr

    def run(self, argv, *, input=None, cwd=None):
        from cairn.kernel.schedkit import RunResult

        self.calls.append({"argv": list(argv), "input": input, "cwd": cwd})
        if list(argv[:2]) == ["crontab", "-l"]:
            return RunResult(0 if self._crontab else 1, self._crontab, "")
        return RunResult(self.returncode, self._stdout, self._stderr)


def _write_schedules(ws: Path) -> None:
    (ws / "schedules.yaml").write_text(
        'nightly:\n  cron: "30 2 * * *"\n  run: [run, hello, --headless, --idempotent]\n',
        encoding="utf-8",
    )


def _inject_runner(monkeypatch, fake: _FakeRunner) -> None:
    from cairn import cli

    monkeypatch.setattr(cli, "_SubprocessRunner", lambda: fake)


def test_schedule_install_cron_pipes_managed_block(hello_ws, monkeypatch):
    _write_schedules(hello_ws)
    fake = _FakeRunner()
    _inject_runner(monkeypatch, fake)
    monkeypatch.chdir(hello_ws)
    rc = main(["schedule", "install", "--backend", "cron"])
    assert rc == int(ExitCode.OK)
    write = next(c for c in fake.calls if c["argv"] == ["crontab", "-"])
    assert "schedule run nightly" in write["input"]  # the managed block, piped to `crontab -`


def test_schedule_launchd_install_list_uninstall_roundtrip(hello_ws, monkeypatch, tmp_path, capsys):
    _write_schedules(hello_ws)
    _inject_runner(monkeypatch, _FakeRunner())
    monkeypatch.chdir(hello_ws)
    ld = tmp_path / "LaunchAgents"

    assert main(["schedule", "install", "--backend", "launchd", "--launchd-dir", str(ld)]) == int(ExitCode.OK)
    assert list(ld.glob("io.cairn.*.plist"))  # plist written to the INJECTED dir, not ~/Library

    capsys.readouterr()
    assert main(["schedule", "list", "--backend", "launchd", "--launchd-dir", str(ld)]) == int(ExitCode.OK)
    assert "nightly" in capsys.readouterr().out

    assert main(["schedule", "uninstall", "--backend", "launchd", "--launchd-dir", str(ld)]) == int(ExitCode.OK)
    assert not list(ld.glob("io.cairn.*.plist"))


def test_schedule_run_propagates_child_exit_verbatim(hello_ws, monkeypatch):
    _write_schedules(hello_ws)
    # 9 is outside the ExitCode enum — proves the child code passes through unremapped.
    fake = _FakeRunner(returncode=9)
    _inject_runner(monkeypatch, fake)
    monkeypatch.chdir(hello_ws)
    rc = main(["schedule", "run", "nightly"])
    assert rc == 9  # the child cairn's exit code, verbatim
    child = fake.calls[-1]
    assert child["argv"][1:] == ["run", "hello", "--headless", "--idempotent"]


def test_schedule_run_reemits_child_output_verbatim(hello_ws, monkeypatch, capsys):
    # A cron-fired halt must produce output: the Runner captures the child's streams, and
    # `schedule run` re-emits them so the host mailer delivers the halt + resume hint.
    _write_schedules(hello_ws)
    fake = _FakeRunner(
        returncode=6,
        stdout="child progress line\n",
        stderr="cairn: halted awaiting a human → /runs/r  (answer + `cairn resume /runs/r`)\n",
    )
    _inject_runner(monkeypatch, fake)
    monkeypatch.chdir(hello_ws)
    rc = main(["schedule", "run", "nightly"])
    captured = capsys.readouterr()
    assert rc == 6
    assert "child progress line" in captured.out
    assert "cairn resume /runs/r" in captured.err  # the resume hint reaches stderr


def test_schedule_run_missing_cairn_binary_is_clean_config_error(hello_ws, monkeypatch, capsys):
    _write_schedules(hello_ws)

    class MissingBinaryRunner:
        def run(self, argv, *, input=None, cwd=None):
            raise FileNotFoundError(2, "No such file or directory", argv[0])

    _inject_runner(monkeypatch, MissingBinaryRunner())
    monkeypatch.chdir(hello_ws)
    rc = main(["schedule", "run", "nightly"])
    err = capsys.readouterr().err
    assert rc == int(ExitCode.CONFIG)  # a clean exit 2, not an uncaught traceback
    assert "cannot execute" in err and "PATH" in err
    assert "console script" in err  # names the remedy


def test_schedule_run_unknown_name_is_config_error(hello_ws, monkeypatch, capsys):
    _write_schedules(hello_ws)
    _inject_runner(monkeypatch, _FakeRunner())
    monkeypatch.chdir(hello_ws)
    rc = main(["schedule", "run", "ghost"])
    assert rc == int(ExitCode.CONFIG)
    assert "no schedule named 'ghost'" in capsys.readouterr().err


def test_schedule_run_requires_a_name(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    rc = main(["schedule", "run"])
    assert rc == int(ExitCode.CONFIG)
    assert "needs a schedule name" in capsys.readouterr().err


def test_schedule_missing_yaml_is_config_error(hello_ws, monkeypatch, capsys):
    _inject_runner(monkeypatch, _FakeRunner())
    monkeypatch.chdir(hello_ws)
    rc = main(["schedule", "install"])
    assert rc == int(ExitCode.CONFIG)
    assert "schedules.yaml" in capsys.readouterr().err


def test_subprocess_runner_actually_shells_out():
    # The real Runner adapter (no fake): proves it shells out and maps stdout/returncode —
    # a regression guard for the `import subprocess` the fake-runner tests never exercised.
    from cairn.cli import _SubprocessRunner

    res = _SubprocessRunner().run([sys.executable, "-c", "import sys; print('hi'); sys.exit(7)"])
    assert res.returncode == 7
    assert res.stdout.strip() == "hi"


# --------------------------------------------------------------------------- #
# _now — the CLI's single clock source.
# --------------------------------------------------------------------------- #


def test_now_returns_aware_utc():
    # _now() feeds run.json created_at / node `at` (stamped Z by trail.format_at), plan
    # {date} templating, doctor, and gatekit. A naive local clock would be *labeled* UTC —
    # a lie by the local offset — so the clock source itself must be aware UTC.
    from datetime import timezone

    from cairn.cli import _now

    dt = _now()
    assert dt.tzinfo is timezone.utc
