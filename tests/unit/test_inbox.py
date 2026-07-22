"""cairn inbox — cross-run judgment drain (FACTORY-PLAN W2 / T9).

Enumerate parked gates+manuals, answer via interactive path, resume through the
shared ``_drive_resume`` helper, origin provenance, drift card, --list/--json/--failed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from cairn.cli import main
from cairn.kernel import newkit
from cairn.kernel.runstate import load_run
from cairn.kernel.types import ExitCode


@pytest.fixture
def hello_ws(tmp_path: Path) -> Path:
    return newkit.new_workspace("demo", tmp_path)


def _run_dir(ws: Path, prefix: str = "") -> Path:
    dirs = sorted(d for d in (ws / "runs").iterdir() if d.is_dir() and d.name.startswith(prefix))
    assert dirs, f"no run dir under {ws / 'runs'}"
    return dirs[-1]


def _path_without_cairn() -> str:
    kept = [
        d
        for d in os.environ.get("PATH", "").split(os.pathsep)
        if not (Path(d) / "cairn").is_file()
    ]
    return os.pathsep.join(kept)


def _write_gateflow(ws: Path, *, defaultless: bool = True) -> None:
    default_line = "" if defaultless else "    default: friendly\n"
    (ws / "pipelines" / "gateflow.yaml").write_text(
        "pipeline: gateflow\n"
        "version: 1\n"
        "run_id: \"gateflow-{date}\"\n"
        "artifacts:\n"
        "  greeting:\n"
        "    path: greeting.txt\n"
        "    validator: validators/nonempty.py\n"
        "  message:\n"
        "    path: message.txt\n"
        "    validator: validators/nonempty.py\n"
        "steps:\n"
        "  - id: greet\n"
        "    run: \"echo hi > greeting.txt\"\n"
        "    produces: [greeting]\n"
        "  - gate: tone\n"
        "    reads: [greeting]\n"
        "    ask: \"What tone?\"\n"
        "    options:\n"
        "      friendly: \"Warm\"\n"
        "      formal: \"Polished\"\n"
        f"{default_line}"
        "  - id: compose\n"
        "    run: \"echo {gate:tone} done > message.txt\"\n"
        "    needs: [greeting, tone]\n"
        "    produces: [message]\n",
        encoding="utf-8",
    )


def _write_manualflow(ws: Path) -> None:
    (ws / "pipelines" / "manualflow.yaml").write_text(
        "pipeline: manualflow\n"
        "version: 1\n"
        "run_id: \"manualflow-{date}\"\n"
        "artifacts:\n"
        "  token: { path: token.txt, validator: validators/nonempty.py }\n"
        "steps:\n"
        "  - id: approve\n"
        "    manual: \"write token.txt\"\n"
        "    produces: [token]\n"
        "  - id: after\n"
        "    run: \"echo done\"\n"
        "    needs: [token]\n",
        encoding="utf-8",
    )


def _write_done_pipeline(ws: Path) -> None:
    (ws / "pipelines" / "doneflow.yaml").write_text(
        "pipeline: doneflow\n"
        "version: 1\n"
        "run_id: \"doneflow-{date}\"\n"
        "steps:\n"
        "  - id: only\n"
        "    run: \"echo ok\"\n",
        encoding="utf-8",
    )


def test_inbox_enumerates_only_parked_judgments(hello_ws, monkeypatch, capsys):
    """runs root with gate-parked + manual-parked + done → lists exactly the two parked."""
    monkeypatch.chdir(hello_ws)
    _write_gateflow(hello_ws)
    _write_manualflow(hello_ws)
    _write_done_pipeline(hello_ws)

    assert main(["run", "gateflow", "--headless"]) == int(ExitCode.NEEDS_HUMAN)
    assert main(["run", "manualflow", "--headless"]) == int(ExitCode.NEEDS_HUMAN)
    assert main(["run", "doneflow", "--headless"]) == int(ExitCode.OK)

    # A non-parked "running" stand-in: mint via --to on a short pipeline leaves done;
    # use a fresh incomplete run by writing a bare run dir is schema-invalid. Exclusion
    # of done is the critical assert; running is excluded by status not gate/halt-6.
    capsys.readouterr()
    rc = main(["inbox", "--list"])
    out = capsys.readouterr().out
    assert rc == int(ExitCode.OK)
    assert "gateflow" in out
    assert "manualflow" in out
    assert "doneflow" not in out
    lines = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("RUN")]
    assert len(lines) == 2


def test_inbox_list_json_and_empty(hello_ws, monkeypatch, capsys):
    monkeypatch.chdir(hello_ws)
    rc = main(["inbox", "--list"])
    assert rc == int(ExitCode.OK)
    assert "inbox empty" in capsys.readouterr().out

    rc = main(["inbox", "--json"])
    assert rc == int(ExitCode.OK)
    assert json.loads(capsys.readouterr().out) == []

    _write_gateflow(hello_ws)
    assert main(["run", "gateflow", "--headless"]) == int(ExitCode.NEEDS_HUMAN)
    capsys.readouterr()
    rc = main(["inbox", "--json"])
    assert rc == int(ExitCode.OK)
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    assert payload[0]["pipeline"] == "gateflow"
    assert payload[0]["gate"] == "tone"
    assert payload[0]["kind"] == "gate"
    assert "friendly" in payload[0]["options"]


def test_inbox_interactive_answer_resume(hello_ws, monkeypatch, capsys):
    """Park a defaultless gate → feed a choice via inbox → run reaches done."""
    monkeypatch.chdir(hello_ws)
    _write_gateflow(hello_ws)
    assert main(["run", "gateflow", "--headless"]) == int(ExitCode.NEEDS_HUMAN)
    rd = _run_dir(hello_ws, "gateflow")
    assert not (rd / "gates" / "tone.json").exists()

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    answers = iter(["friendly"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))

    capsys.readouterr()
    rc = main(["inbox"])
    assert rc == int(ExitCode.OK)
    decision = json.loads((rd / "gates" / "tone.json").read_text(encoding="utf-8"))
    assert decision["choice"] == "friendly" and decision["by"] == "external"
    doc = load_run(rd)
    assert doc["status"] == "done"
    assert (rd / "message.txt").is_file()


def test_inbox_answer_resume_with_origin_sweep(hello_ws, monkeypatch, capsys):
    """Park via trigger drain (origin stamped) → inbox confirm → sweep retires to .done/."""
    (hello_ws / "triggers.yaml").write_text(
        "handle-reply:\n  pipeline: on-event-manual\n  watch: inbox/replies\n",
        encoding="utf-8",
    )
    (hello_ws / "pipelines" / "on-event-manual.yaml").write_text(
        "pipeline: on-event-manual\n"
        "version: 1\n"
        "params:\n"
        "  event: { type: string, required: true }\n"
        "artifacts:\n"
        "  token:\n"
        "    path: token.txt\n"
        "    validator: validators/nonempty.py\n"
        "steps:\n"
        "  - id: approve\n"
        "    manual: \"write token.txt after reviewing {params.event}\"\n"
        "    produces: [token]\n",
        encoding="utf-8",
    )
    inbox = hello_ws / "inbox" / "replies"
    inbox.mkdir(parents=True)
    (inbox / "need-human.json").write_text('{"ticket": 1}', encoding="utf-8")
    monkeypatch.chdir(hello_ws)
    monkeypatch.setenv("PATH", _path_without_cairn())

    rc1 = main(["trigger", "run", "handle-reply"])
    assert rc1 == int(ExitCode.OK)
    assert (inbox / ".waiting" / "need-human.json").is_file()
    run_dir = hello_ws / "runs" / "handle-reply-need-human"
    assert run_dir.is_dir()
    doc = load_run(run_dir)
    assert doc.get("origin") == "trigger:handle-reply"

    # Human does the manual work, then inbox confirms resume.
    (run_dir / "token.txt").write_text("approved\n", encoding="utf-8")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p="": "")  # Enter = resume manual

    capsys.readouterr()
    rc = main(["inbox"])
    assert rc == int(ExitCode.OK)
    assert load_run(run_dir)["status"] == "done"
    assert (inbox / ".done" / "need-human.json").is_file()
    assert not (inbox / ".waiting" / "need-human.json").exists()


def test_inbox_drift_card_refuses_without_force(hello_ws, monkeypatch, capsys):
    """Park → mutate pipeline → inbox resume attempt shows DRIFT CARD; force resumes."""
    monkeypatch.chdir(hello_ws)
    _write_gateflow(hello_ws)
    assert main(["run", "gateflow", "--headless"]) == int(ExitCode.NEEDS_HUMAN)
    rd = _run_dir(hello_ws, "gateflow")

    # Mutate pipeline so hash drifts.
    pfile = hello_ws / "pipelines" / "gateflow.yaml"
    pfile.write_text(pfile.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    # First choice answers the gate; then drift force prompt gets "s" (leave parked).
    answers = iter(["friendly", "s"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))

    capsys.readouterr()
    rc = main(["inbox"])
    out = capsys.readouterr()
    assert "DRIFT CARD" in out.out or "DRIFT CARD" in out.err or "pipeline changed" in out.err.lower() or "pipeline" in out.err
    # Gate was answered but run stays parked (not done) because resume refused.
    assert (rd / "gates" / "tone.json").is_file()
    assert load_run(rd)["status"] != "done"

    # Force path: re-enter inbox (gate already answered → card still there as halted-6
    # until resume succeeds). Actually after answer the gate is answered; on resume the
    # walker skips the gate. Status still halted until successful resume.
    # Re-list should still show the run as parked.
    capsys.readouterr()
    rc_list = main(["inbox", "--list"])
    listed = capsys.readouterr().out
    assert rc_list == int(ExitCode.OK)
    assert "gateflow" in listed

    # Force resume via inbox: gate already answered so card kind is still gate/manual
    # pending-less? Last gate-pending still exists; kind=gate but is_answered True.
    # Answering again would fail overwrite — interactive path calls answer_gate again!
    # Problem: after first answer, re-entering and choosing "friendly" again will refuse
    # overwrite in answer_gate? answer_gate does NOT check is_answered — it overwrites
    # the decision file. The `cairn gate` verb refuses overwrite; answer_gate itself
    # rewrites. So a second "friendly" would re-answer then drift again.
    #
    # For force path: feed friendly + f
    answers2 = iter(["friendly", "f"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers2))
    capsys.readouterr()
    rc2 = main(["inbox"])
    assert rc2 == int(ExitCode.OK)
    assert load_run(rd)["status"] == "done"


def test_origin_recorded_on_run_flag(hello_ws, monkeypatch):
    """``cairn run --origin`` stamps run.json; inbox reads it."""
    monkeypatch.chdir(hello_ws)
    _write_gateflow(hello_ws)
    rc = main(["run", "gateflow", "--headless", "--origin", "trigger:demo"])
    assert rc == int(ExitCode.NEEDS_HUMAN)
    rd = _run_dir(hello_ws, "gateflow")
    doc = load_run(rd)
    assert doc.get("origin") == "trigger:demo"

    # inbox --json surfaces origin
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["inbox", "--json"]) == int(ExitCode.OK)
    payload = json.loads(buf.getvalue())
    assert payload[0]["origin"] == "trigger:demo"


def test_inbox_failed_listing_readonly(hello_ws, monkeypatch, capsys):
    """--failed lists .failed/ items with run pointers; does not retry."""
    (hello_ws / "triggers.yaml").write_text(
        "handle-reply:\n  pipeline: on-event-fail\n  watch: inbox/replies\n",
        encoding="utf-8",
    )
    (hello_ws / "pipelines" / "on-event-fail.yaml").write_text(
        "pipeline: on-event-fail\n"
        "version: 1\n"
        "params:\n"
        "  event: { type: string, required: true }\n"
        "steps:\n"
        "  - id: boom\n"
        '    run: "false"\n',
        encoding="utf-8",
    )
    inbox = hello_ws / "inbox" / "replies"
    inbox.mkdir(parents=True)
    (inbox / "bad.json").write_text('{"bad": true}', encoding="utf-8")
    monkeypatch.chdir(hello_ws)
    monkeypatch.setenv("PATH", _path_without_cairn())

    assert main(["trigger", "run", "handle-reply"]) != 0
    assert (inbox / ".failed" / "bad.json").is_file()

    capsys.readouterr()
    rc = main(["inbox", "--failed"])
    out = capsys.readouterr().out
    assert rc == int(ExitCode.OK)
    assert "bad.json" in out
    assert "handle-reply" in out
    assert "trigger retry" in out.lower() or "future" in out.lower()
    # Still failed — no retry performed.
    assert (inbox / ".failed" / "bad.json").is_file()


def test_inbox_subcommand_registered():
    from cairn.cli import SUBCOMMANDS

    assert "inbox" in SUBCOMMANDS
