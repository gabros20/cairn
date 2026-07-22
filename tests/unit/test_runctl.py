"""runctl — RunController entrance library (W0.2): decision tree + typed refusals.

No CLI process: call mint_new / resume_existing / resolve_run directly and pin
message text, remedies, and exit codes. CLI wiring stays in test_cli.py.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

import cairn
from cairn.kernel import newkit
from cairn.kernel.plan import plan as build_plan
from cairn.kernel.runctl import (
    AlreadyDone,
    Minted,
    Refusal,
    Resumable,
    mint_new,
    preflight_tools,
    resolve_run,
    resume_existing,
)
from cairn.kernel.runstate import load_run, update_run
from cairn.kernel.types import ExitCode
from cairn.kernel.walk import bootstrap_run


def _now() -> datetime:
    return datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)


def _phash(ws: Path, pipeline: str) -> str:
    f = ws / "pipelines" / f"{pipeline}.yaml"
    return "sha256:" + hashlib.sha256(f.read_bytes()).hexdigest()


@pytest.fixture
def hello_ws(tmp_path: Path) -> Path:
    return newkit.new_workspace("demo", tmp_path)


def _plan(ws: Path, pipeline: str = "hello"):
    return build_plan(ws, pipeline, {}, now=_now(), headless=True)


def _minted_run(ws: Path, pipeline: str = "hello") -> Path:
    p = _plan(ws, pipeline)
    ph = _phash(ws, pipeline)
    return bootstrap_run(ws, p, now=_now(), runs_root=ws / "runs", pipeline_hash=ph)


# --------------------------------------------------------------------------- #
# resume_existing — load fail-loud, drift, version
# --------------------------------------------------------------------------- #


def test_resume_existing_unreadable_run_json_is_config_refusal(hello_ws, tmp_path):
    rd = tmp_path / "broken"
    rd.mkdir()
    (rd / "run.json").write_text("{corrupt", encoding="utf-8")
    result = resume_existing(
        rd, ws=hello_ws, phash="sha256:abc", pipeline="hello", force=False
    )
    assert isinstance(result, Refusal)
    assert result.code == ExitCode.CONFIG
    assert "cannot read" in result.message and "run.json" in result.message
    assert "records no cairn version" not in result.message


def test_resume_existing_pipeline_drift_refuses_with_force_remedy(hello_ws):
    rd = _minted_run(hello_ws)
    pfile = hello_ws / "pipelines" / "hello.yaml"
    pfile.write_text(pfile.read_text() + "\n# drift\n", encoding="utf-8")
    ph = _phash(hello_ws, "hello")
    result = resume_existing(rd, ws=hello_ws, phash=ph, pipeline="hello", force=False)
    assert isinstance(result, Refusal)
    assert result.code == ExitCode.CONFIG
    assert "hash drift" in result.message
    assert result.remedy is not None
    assert f"cairn resume {rd} --force" in result.remedy
    assert result.remedy in result.message  # full stderr text embeds the remedy


def test_resume_existing_pipeline_drift_force_warns_and_proceeds(hello_ws, capsys):
    rd = _minted_run(hello_ws)
    pfile = hello_ws / "pipelines" / "hello.yaml"
    pfile.write_text(pfile.read_text() + "\n# drift\n", encoding="utf-8")
    ph = _phash(hello_ws, "hello")
    result = resume_existing(rd, ws=hello_ws, phash=ph, pipeline="hello", force=True)
    assert isinstance(result, Resumable)
    assert result.run_dir == rd
    err = capsys.readouterr().err
    assert "warning" in err and "pipeline-hash drift" in err and "--force" in err


def test_resume_existing_cross_major_version_refuses(hello_ws):
    rd = _minted_run(hello_ws)
    update_run(rd, lambda doc: doc.update({"cairn_version": "9.0.0"}))
    ph = _phash(hello_ws, "hello")
    result = resume_existing(rd, ws=hello_ws, phash=ph, pipeline="hello", force=False)
    assert isinstance(result, Refusal)
    assert result.code == ExitCode.CONFIG
    assert "9.0.0" in result.message and cairn.__version__ in result.message
    assert result.remedy is not None
    assert f"cairn resume {rd} --force" in result.remedy


def test_resume_existing_cross_major_force_proceeds(hello_ws, capsys):
    rd = _minted_run(hello_ws)
    update_run(rd, lambda doc: doc.update({"cairn_version": "9.0.0"}))
    ph = _phash(hello_ws, "hello")
    result = resume_existing(rd, ws=hello_ws, phash=ph, pipeline="hello", force=True)
    assert isinstance(result, Resumable)
    err = capsys.readouterr().err
    assert "--force" in err and "version drift" in err


def test_resume_existing_clean_path_returns_resumable(hello_ws, capsys):
    rd = _minted_run(hello_ws)
    ph = _phash(hello_ws, "hello")
    result = resume_existing(rd, ws=hello_ws, phash=ph, pipeline="hello", force=False)
    assert isinstance(result, Resumable)
    assert result.run_dir == rd
    assert result.run_doc["pipeline"] == "hello"
    assert capsys.readouterr().err == ""  # silent when versions/hashes match


# --------------------------------------------------------------------------- #
# mint_new — preflight before bootstrap
# --------------------------------------------------------------------------- #


def test_mint_new_preflight_refusal_mints_nothing(hello_ws):
    from dataclasses import replace

    from cairn.kernel.plan import ToolRequirement

    p = replace(
        _plan(hello_ws),
        tool_requirements=(
            ToolRequirement(
                tool="needsit",
                check="false",
                targets=("greet",),
                install="brew install needsit",
            ),
        ),
    )
    runs_root = hello_ws / "runs"
    result = mint_new(
        hello_ws, p, now=_now(), pipeline_hash=_phash(hello_ws, "hello"), runs_root=runs_root
    )
    assert isinstance(result, Refusal)
    assert result.code == ExitCode.CONFIG
    assert "needsit" in result.message and "greet" in result.message
    assert "brew install needsit" in result.message
    assert "cairn doctor" in result.message
    assert not runs_root.exists() or not any(runs_root.iterdir())


def test_mint_new_success_creates_run_dir(hello_ws):
    p = _plan(hello_ws)
    runs_root = hello_ws / "runs"
    result = mint_new(
        hello_ws, p, now=_now(), pipeline_hash=_phash(hello_ws, "hello"), runs_root=runs_root
    )
    assert isinstance(result, Minted)
    assert (result.run_dir / "run.json").is_file()
    assert load_run(result.run_dir)["pipeline"] == "hello"


# --------------------------------------------------------------------------- #
# resolve_run — --run-dir / --idempotent / fresh decision tree
# --------------------------------------------------------------------------- #


def test_resolve_run_fresh_mints(hello_ws):
    p = _plan(hello_ws)
    result = resolve_run(
        hello_ws,
        p,
        now=_now(),
        pipeline_hash=_phash(hello_ws, "hello"),
        runs_root=hello_ws / "runs",
    )
    assert isinstance(result, Minted)
    assert (result.run_dir / "run.json").is_file()


def test_resolve_run_run_dir_fresh_mints(hello_ws, tmp_path):
    p = _plan(hello_ws)
    target = tmp_path / "explicit-run"
    result = resolve_run(
        hello_ws,
        p,
        now=_now(),
        pipeline_hash=_phash(hello_ws, "hello"),
        run_dir=target,
    )
    assert isinstance(result, Minted)
    assert result.run_dir == target.resolve()
    assert (target / "run.json").is_file()


def test_resolve_run_run_dir_existing_resumes(hello_ws):
    rd = _minted_run(hello_ws)
    p = _plan(hello_ws)
    result = resolve_run(
        hello_ws,
        p,
        now=_now(),
        pipeline_hash=_phash(hello_ws, "hello"),
        run_dir=rd,
    )
    assert isinstance(result, Resumable)
    assert result.run_dir == rd.resolve()


def test_resolve_run_run_dir_existing_drift_refuses(hello_ws):
    rd = _minted_run(hello_ws)
    pfile = hello_ws / "pipelines" / "hello.yaml"
    pfile.write_text(pfile.read_text() + "\n# drift\n", encoding="utf-8")
    p = _plan(hello_ws)
    result = resolve_run(
        hello_ws,
        p,
        now=_now(),
        pipeline_hash=_phash(hello_ws, "hello"),
        run_dir=rd,
    )
    assert isinstance(result, Refusal)
    assert "hash drift" in result.message
    assert f"cairn resume {rd.resolve()} --force" in (result.remedy or "")


def test_resolve_run_idempotent_complete_is_already_done(hello_ws):
    p = _plan(hello_ws)
    ph = _phash(hello_ws, "hello")
    # Mint via resolve, then mark done and re-resolve with --idempotent.
    first = resolve_run(
        hello_ws, p, now=_now(), pipeline_hash=ph, runs_root=hello_ws / "runs"
    )
    assert isinstance(first, Minted)
    update_run(first.run_dir, lambda doc: doc.update({"status": "done"}))
    second = resolve_run(
        hello_ws,
        p,
        now=_now(),
        pipeline_hash=ph,
        runs_root=hello_ws / "runs",
        idempotent=True,
    )
    assert isinstance(second, AlreadyDone)
    assert second.run_dir == first.run_dir


def test_resolve_run_idempotent_incomplete_resumes(hello_ws):
    p = _plan(hello_ws)
    ph = _phash(hello_ws, "hello")
    first = resolve_run(
        hello_ws, p, now=_now(), pipeline_hash=ph, runs_root=hello_ws / "runs"
    )
    assert isinstance(first, Minted)
    # status stays "running" (incomplete)
    second = resolve_run(
        hello_ws,
        p,
        now=_now(),
        pipeline_hash=ph,
        runs_root=hello_ws / "runs",
        idempotent=True,
    )
    assert isinstance(second, Resumable)
    assert second.run_dir == first.run_dir


def test_resolve_run_idempotent_none_mints_fresh(hello_ws):
    p = _plan(hello_ws)
    result = resolve_run(
        hello_ws,
        p,
        now=_now(),
        pipeline_hash=_phash(hello_ws, "hello"),
        runs_root=hello_ws / "runs",
        idempotent=True,
    )
    assert isinstance(result, Minted)


def test_resolve_run_idempotent_on_run_dir_done_short_circuits(hello_ws):
    rd = _minted_run(hello_ws)
    update_run(rd, lambda doc: doc.update({"status": "done"}))
    p = _plan(hello_ws)
    result = resolve_run(
        hello_ws,
        p,
        now=_now(),
        pipeline_hash=_phash(hello_ws, "hello"),
        run_dir=rd,
        idempotent=True,
    )
    assert isinstance(result, AlreadyDone)
    assert result.run_dir == rd.resolve()


def test_preflight_tools_all_pass_is_silent(hello_ws, capsys):
    p = _plan(hello_ws)
    assert preflight_tools(p) is None
    assert capsys.readouterr().err == ""
