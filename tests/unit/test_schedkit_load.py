"""schedules.yaml parsing + validation → typed Schedule objects, precise CONFIG errors.

Behaviour tests against the public surface (load_schedules / Schedule) — SCHEDULING.md §1.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cairn.kernel.errors import ConfigError
from cairn.kernel.schedkit import Schedule, load_schedules


def _workspace(tmp_path: Path, schedules_yaml: str, *, pipelines=("brease-rebrand",)) -> Path:
    """A minimal workspace: schedules.yaml + a pipelines/ dir with the named pipelines."""
    (tmp_path / "schedules.yaml").write_text(textwrap.dedent(schedules_yaml), encoding="utf-8")
    pdir = tmp_path / "pipelines"
    pdir.mkdir(exist_ok=True)
    for name in pipelines:
        (pdir / f"{name}.yaml").write_text("pipeline: x\nsteps: []\n", encoding="utf-8")
    return tmp_path


def test_load_one_schedule(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        weekly:
          cron: "0 3 * * 1"
          run: [run, brease-rebrand, --param, url=https://a.test, --headless]
        """,
    )
    scheds = load_schedules(ws)
    assert list(scheds) == ["weekly"]
    s = scheds["weekly"]
    assert isinstance(s, Schedule)
    assert s.name == "weekly"
    assert s.cron == "0 3 * * 1"
    assert s.run == ("run", "brease-rebrand", "--param", "url=https://a.test", "--headless")


def test_missing_file_is_config_error(tmp_path):
    with pytest.raises(ConfigError, match="no schedules.yaml"):
        load_schedules(tmp_path)


def test_empty_file_is_no_schedules(tmp_path):
    ws = _workspace(tmp_path, "")
    assert load_schedules(ws) == {}


def test_unknown_key_fails_loudly(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        weekly:
          cron: "0 3 * * 1"
          run: [run, brease-rebrand]
          when: "someday"
        """,
    )
    with pytest.raises(ConfigError, match="unknown key 'when'"):
        load_schedules(ws)


def test_bad_cron_fails_at_parse_time(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        weekly:
          cron: "0 99 * * 1"
          run: [run, brease-rebrand]
        """,
    )
    with pytest.raises(ConfigError, match="invalid cron"):
        load_schedules(ws)


def test_unknown_pipeline_fails_at_parse_time(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        weekly:
          cron: "0 3 * * 1"
          run: [run, no-such-pipeline, --headless]
        """,
    )
    with pytest.raises(ConfigError, match="unknown pipeline 'no-such-pipeline'"):
        load_schedules(ws)


def test_disallowed_verb_fails(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        weekly:
          cron: "0 3 * * 1"
          run: [doctor]
        """,
    )
    with pytest.raises(ConfigError, match="not allowed"):
        load_schedules(ws)


def test_run_missing_pipeline_positional_fails(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        weekly:
          cron: "0 3 * * 1"
          run: [run, --headless]
        """,
    )
    with pytest.raises(ConfigError, match="requires a pipeline name"):
        load_schedules(ws)


def test_non_string_run_token_fails(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        weekly:
          cron: "0 3 * * 1"
          run: [run, brease-rebrand, 4]
        """,
    )
    with pytest.raises(ConfigError, match="must be a string"):
        load_schedules(ws)


def test_gc_and_resume_verbs_need_no_pipeline(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        cleanup:
          cron: "0 4 * * 0"
          run: [gc, --keep-days, "30"]
        catchup:
          cron: "0 5 * * *"
          run: [resume, acme-redesign-20260703, --headless]
        """,
    )
    scheds = load_schedules(ws)
    assert set(scheds) == {"cleanup", "catchup"}
    assert scheds["cleanup"].run[0] == "gc"


def test_trigger_verb_parses_and_is_not_forced_headless(tmp_path):
    # Addendum 2 (T5, post-T3): T3's cron-refusal fallback message documents a
    # schedules.yaml entry invoking `trigger run <name>` (TRIGGERS.md §3) — this proves
    # that entry actually loads. `trigger` gets the same non-interactive exemption as
    # `gc`: its own child run is already --headless by construction, so no --headless
    # token is required on THIS argv (unlike run/batch/resume).
    ws = _workspace(
        tmp_path,
        """
        poll-inbox:
          cron: "*/5 * * * *"
          run: [trigger, run, handle-reply]
        """,
    )
    scheds = load_schedules(ws)
    assert set(scheds) == {"poll-inbox"}
    assert scheds["poll-inbox"].run == ("trigger", "run", "handle-reply")


def test_run_without_headless_is_rejected(tmp_path):
    # SCHEDULING.md §4: scheduled run/batch/resume are headless runs — required, not optional.
    ws = _workspace(
        tmp_path,
        """
        weekly:
          cron: "0 3 * * 1"
          run: [run, brease-rebrand, --param, url=https://a.test]
        """,
    )
    with pytest.raises(ConfigError, match="--headless"):
        load_schedules(ws)


def test_batch_without_headless_is_rejected(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        fleet:
          cron: "0 3 * * 1"
          run: [batch, brease-rebrand, --params-file, sites.jsonl]
        """,
    )
    with pytest.raises(ConfigError, match="--headless"):
        load_schedules(ws)


def test_resume_without_headless_is_rejected(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        catchup:
          cron: "0 5 * * *"
          run: [resume, acme-redesign-20260703]
        """,
    )
    with pytest.raises(ConfigError, match="--headless"):
        load_schedules(ws)


def test_gc_is_exempt_from_headless(tmp_path):
    # gc is inherently non-interactive; requiring --headless would be nonsense.
    ws = _workspace(
        tmp_path,
        """
        cleanup:
          cron: "0 4 * * 0"
          run: [gc, --keep-days, "30"]
        """,
    )
    assert load_schedules(ws)["cleanup"].run == ("gc", "--keep-days", "30")
