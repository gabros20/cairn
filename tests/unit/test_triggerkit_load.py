"""triggers.yaml parsing + validation → typed Trigger objects, precise CONFIG errors.

Behaviour tests against the public surface (load_triggers / Trigger) — TRIGGERS-PLAN.md §1.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cairn.kernel.errors import ConfigError
from cairn.kernel.triggerkit import Trigger, load_triggers


def _workspace(tmp_path: Path, triggers_yaml: str | None, *, pipelines=("handle-reply",)) -> Path:
    """A minimal workspace: triggers.yaml (unless None) + a pipelines/ dir."""
    if triggers_yaml is not None:
        (tmp_path / "triggers.yaml").write_text(textwrap.dedent(triggers_yaml), encoding="utf-8")
    pdir = tmp_path / "pipelines"
    pdir.mkdir(exist_ok=True)
    for name in pipelines:
        (pdir / f"{name}.yaml").write_text("pipeline: x\nsteps: []\n", encoding="utf-8")
    return tmp_path


def test_load_one_trigger_defaults(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies/
        """,
    )
    triggers = load_triggers(ws)
    assert list(triggers) == ["handle-reply"]
    t = triggers["handle-reply"]
    assert isinstance(t, Trigger)
    assert t.name == "handle-reply"
    assert t.pipeline == "handle-reply"
    assert t.watch == "inbox/replies/"
    assert t.param == "event"
    assert t.glob == "*"
    assert t.on_done == "done"


def test_load_trigger_with_all_fields(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          param: payload
          glob: "*.json"
          on_done: delete
        """,
    )
    t = load_triggers(ws)["handle-reply"]
    assert t.param == "payload"
    assert t.glob == "*.json"
    assert t.on_done == "delete"


def test_missing_file_is_no_triggers(tmp_path):
    # Unlike schedules.yaml, triggers.yaml is optional infrastructure.
    assert load_triggers(tmp_path) == {}


def test_empty_file_is_no_triggers(tmp_path):
    ws = _workspace(tmp_path, "")
    assert load_triggers(ws) == {}


def test_non_mapping_top_level_fails(tmp_path):
    ws = _workspace(tmp_path, "- not\n- a\n- mapping\n")
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_triggers(ws)


def test_invalid_yaml_fails(tmp_path):
    ws = _workspace(tmp_path, "handle-reply: [unterminated\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_triggers(ws)


def test_non_mapping_entry_fails(tmp_path):
    ws = _workspace(tmp_path, "handle-reply: not-a-mapping\n")
    with pytest.raises(ConfigError, match="must be a mapping with 'pipeline' and 'watch'"):
        load_triggers(ws)


def test_unknown_key_fails_loudly(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          extra: nope
        """,
    )
    with pytest.raises(ConfigError, match="unknown key 'extra'"):
        load_triggers(ws)


def test_missing_pipeline_fails(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          watch: inbox/replies
        """,
    )
    with pytest.raises(ConfigError, match="requires a non-empty string 'pipeline'"):
        load_triggers(ws)


def test_unknown_pipeline_fails_at_parse_time(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: no-such-pipeline
          watch: inbox/replies
        """,
    )
    with pytest.raises(ConfigError, match="unknown pipeline 'no-such-pipeline'"):
        load_triggers(ws)


def test_missing_watch_fails(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
        """,
    )
    with pytest.raises(ConfigError, match="requires a non-empty string 'watch'"):
        load_triggers(ws)


def test_absolute_watch_fails(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: /etc/inbox
        """,
    )
    with pytest.raises(ConfigError, match="must be workspace-relative, not absolute"):
        load_triggers(ws)


def test_escaping_watch_fails(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: ../outside
        """,
    )
    with pytest.raises(ConfigError, match="must not escape the workspace"):
        load_triggers(ws)


def test_bad_on_done_fails(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          on_done: archive
        """,
    )
    with pytest.raises(ConfigError, match="'on_done' must be one of"):
        load_triggers(ws)


def test_multiple_triggers(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
        handle-webhook:
          pipeline: handle-webhook
          watch: inbox/webhooks
        """,
        pipelines=("handle-reply", "handle-webhook"),
    )
    triggers = load_triggers(ws)
    assert set(triggers) == {"handle-reply", "handle-webhook"}
