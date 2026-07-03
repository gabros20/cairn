"""Render-only backends: crontab block, launchd plist, systemd unit+timer.

Pure string generation, fully offline — these never touch the host scheduler.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from cairn.kernel.errors import ConfigError
from cairn.kernel.schedkit import (
    Schedule,
    merge_cron,
    render_cron,
    render_launchd,
    render_systemd,
    strip_cron,
)

WS = Path("/ws/acme")


def _sched(cron="0 3 * * 1", name="weekly", run=("run", "brease-rebrand")):
    return Schedule(name=name, cron=cron, run=tuple(run))


# --- cron -------------------------------------------------------------------


def test_render_cron_is_marker_fenced_one_line_per_schedule():
    block = render_cron(
        {"weekly": _sched(), "nightly": _sched(cron="30 2 * * *", name="nightly")},
        workspace_dir=WS,
        cairn_bin="cairn",
    )
    lines = block.splitlines()
    assert lines[0].startswith("# >>>") and lines[-1].startswith("# <<<")
    body = [ln for ln in lines if not ln.startswith("#")]
    assert len(body) == 2
    # the installed host entry is always `cairn schedule run <name>`, never the expanded argv
    assert body[0].startswith("0 3 * * 1 ")
    assert "schedule run weekly" in body[0]
    assert "brease-rebrand" not in block  # argv stays in schedules.yaml, not the crontab


def test_merge_cron_preserves_foreign_entries_and_replaces_managed_block():
    existing = "0 0 * * * /usr/bin/backup\n"
    block = render_cron({"weekly": _sched()}, workspace_dir=WS)
    merged = merge_cron(existing, block)
    assert "/usr/bin/backup" in merged
    assert "schedule run weekly" in merged
    # re-merging a new block replaces the old managed region, not duplicates it
    block2 = render_cron({"nightly": _sched(name="nightly")}, workspace_dir=WS)
    remerged = merge_cron(merged, block2)
    assert "schedule run weekly" not in remerged
    assert "schedule run nightly" in remerged
    assert "/usr/bin/backup" in remerged
    assert remerged.count("# >>>") == 1


def test_strip_cron_removes_only_the_managed_block():
    existing = "0 0 * * * /usr/bin/backup\n"
    merged = merge_cron(existing, render_cron({"weekly": _sched()}, workspace_dir=WS))
    stripped = strip_cron(merged)
    assert "/usr/bin/backup" in stripped
    assert "schedule run weekly" not in stripped
    assert "# >>>" not in stripped


# --- launchd ----------------------------------------------------------------


def test_render_launchd_is_valid_plist_with_calendar_and_argv():
    xml = render_launchd(_sched(cron="30 2 * * *"), workspace_dir=WS, cairn_bin="cairn")
    doc = plistlib.loads(xml.encode("utf-8"))
    assert doc["Label"] == "io.cairn.weekly"
    assert doc["ProgramArguments"] == ["cairn", "schedule", "run", "weekly"]
    assert doc["WorkingDirectory"] == str(WS)
    assert doc["StartCalendarInterval"] == {"Minute": 30, "Hour": 2}


def test_render_launchd_expands_lists_to_interval_array():
    xml = render_launchd(_sched(cron="0 9,17 * * 1"), workspace_dir=WS)
    doc = plistlib.loads(xml.encode("utf-8"))
    intervals = doc["StartCalendarInterval"]
    assert isinstance(intervals, list)
    assert {"Minute": 0, "Hour": 9, "Weekday": 1} in intervals
    assert {"Minute": 0, "Hour": 17, "Weekday": 1} in intervals


# --- systemd ----------------------------------------------------------------


def test_render_systemd_service_and_timer():
    service, timer = render_systemd(_sched(cron="0 3 * * 1"), workspace_dir=WS, cairn_bin="cairn")
    assert "[Service]" in service
    assert "Type=oneshot" in service
    assert "ExecStart=cairn schedule run weekly" in service
    assert f"WorkingDirectory={WS}" in service

    assert "[Timer]" in timer
    assert "OnCalendar=Mon *-*-* 03:00:00" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_render_systemd_oncalendar_wildcards_and_lists():
    _, timer = render_systemd(_sched(cron="30 2 * * *"), workspace_dir=WS)
    assert "OnCalendar=*-*-* 02:30:00" in timer  # no DOW prefix when dow is wildcard
    _, timer2 = render_systemd(_sched(cron="0 9,17 * * *"), workspace_dir=WS)
    assert "OnCalendar=*-*-* 09,17:00:00" in timer2


# --- DOM+DOW both restricted: cron means OR, launchd/systemd can't → fail loud ----


def test_launchd_rejects_both_dom_and_dow_restricted():
    # `0 0 1 * 1` in cron fires on the 1st OR on Mondays; launchd's array is AND. Refuse.
    with pytest.raises(ConfigError, match="day-of-month and day-of-week"):
        render_launchd(_sched(cron="0 0 1 * 1"), workspace_dir=WS)


def test_systemd_rejects_both_dom_and_dow_restricted():
    with pytest.raises(ConfigError, match="day-of-month and day-of-week"):
        render_systemd(_sched(cron="0 0 1 * 1"), workspace_dir=WS)


def test_cron_still_accepts_both_dom_and_dow_restricted():
    # the cron backend passes the expression through verbatim — OR semantics preserved
    block = render_cron({"both": _sched(cron="0 0 1 * 1", name="both")}, workspace_dir=WS)
    assert "0 0 1 * 1 " in block
