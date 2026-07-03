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


@pytest.mark.parametrize("cmd", ["batch", "learnings", "gc", "schedule"])
def test_stub_verbs_not_implemented(cmd, capsys):
    rc = main([cmd])
    assert rc == int(ExitCode.CONFIG)
    assert "not implemented" in capsys.readouterr().err


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
