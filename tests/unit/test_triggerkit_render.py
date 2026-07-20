"""Render-only backends: launchd WatchPaths plist, systemd .path + .service unit pair.

Pure string generation, fully offline — these never touch the host watcher
(docs/TRIGGERS-PLAN.md §3).
"""

from __future__ import annotations

import plistlib
from pathlib import Path

from cairn.kernel.triggerkit import (
    Trigger,
    render_trigger_launchd,
    render_trigger_systemd,
    trigger_launchd_label,
    trigger_systemd_unit_names,
)

WS = Path("/ws/acme")


def _trigger(name="handle-reply", pipeline="handle-reply", watch="inbox/replies", **kw):
    return Trigger(name=name, pipeline=pipeline, watch=watch, **kw)


# --- label / unit-name helpers ------------------------------------------------


def test_trigger_launchd_label_is_namespaced_under_trigger():
    assert trigger_launchd_label("handle-reply") == "io.cairn.trigger.handle-reply"


def test_trigger_systemd_unit_names_are_namespaced_under_trigger():
    assert trigger_systemd_unit_names("handle-reply") == (
        "cairn-trigger-handle-reply.path",
        "cairn-trigger-handle-reply.service",
    )


def test_trigger_and_schedule_namespaces_never_collide_on_a_shared_name():
    # a schedule and a trigger named identically must never produce the same unit/label
    from cairn.kernel.schedkit import launchd_label, systemd_unit_names

    assert trigger_launchd_label("weekly") != launchd_label("weekly")
    trig_path, trig_service = trigger_systemd_unit_names("weekly")
    sched_service, sched_timer = systemd_unit_names("weekly")
    assert trig_path not in (sched_service, sched_timer)
    assert trig_service not in (sched_service, sched_timer)


# --- launchd --------------------------------------------------------------


def test_render_trigger_launchd_is_valid_plist_with_watchpaths_and_argv():
    xml = render_trigger_launchd(_trigger(), WS, "cairn")
    doc = plistlib.loads(xml.encode("utf-8"))
    assert doc["Label"] == "io.cairn.trigger.handle-reply"
    assert doc["ProgramArguments"] == [
        "cairn",
        "trigger",
        "run",
        "handle-reply",
        "--workspace",
        str(WS),
    ]
    assert doc["WatchPaths"] == [str(WS / "inbox/replies")]
    assert doc["ThrottleInterval"] == 10


def test_render_trigger_launchd_watch_dir_is_absolute_and_resolved():
    trig = _trigger(name="ingest", watch="inbox/ingest")
    xml = render_trigger_launchd(trig, WS, "cairn")
    doc = plistlib.loads(xml.encode("utf-8"))
    watch = Path(doc["WatchPaths"][0])
    assert watch.is_absolute()
    assert watch == WS / "inbox/ingest"


# --- systemd ----------------------------------------------------------------


def test_render_trigger_systemd_path_and_service():
    path_unit, service_unit = render_trigger_systemd(_trigger(), WS, "cairn")

    assert "[Path]" in path_unit
    assert f"DirectoryNotEmpty={WS / 'inbox/replies'}" in path_unit
    assert "Unit=cairn-trigger-handle-reply.service" in path_unit
    assert "[Install]" in path_unit
    assert "WantedBy=default.target" in path_unit

    assert "[Service]" in service_unit
    assert "Type=oneshot" in service_unit
    assert f"WorkingDirectory={WS}" in service_unit
    assert (
        "ExecStart=cairn trigger run handle-reply --workspace /ws/acme" in service_unit
    )
    # the oneshot .service itself carries no [Install] — only the .path activates it,
    # mirroring schedkit's timer/service asymmetry
    assert "[Install]" not in service_unit


def test_render_trigger_systemd_argv_is_the_stable_entry_never_expanded_params():
    trig = _trigger(name="ingest", pipeline="handle-reply", watch="inbox/ingest", param="event")
    _, service_unit = render_trigger_systemd(trig, WS, "cairn")
    assert "ExecStart=cairn trigger run ingest --workspace /ws/acme" in service_unit
    # editing triggers.yaml (pipeline/param/glob/on_done) must change behavior without
    # re-sync — none of those fields ever leak into the rendered argv
    assert "handle-reply" not in service_unit.split("ExecStart=")[1]
    assert "event" not in service_unit.split("ExecStart=")[1]


def test_render_trigger_systemd_quotes_workspace_with_spaces_and_non_ascii():
    ws = Path("/ws/acmé co/root dir")
    trig = _trigger(name="ingest", watch="inbox/ingest")
    path_unit, service_unit = render_trigger_systemd(trig, ws, "cairn")

    # DirectoryNotEmpty= is a single unsplit value — no quoting needed or added
    assert f"DirectoryNotEmpty={ws / 'inbox/ingest'}" in path_unit
    # ExecStart= IS word-split by systemd, so the space-and-accent-bearing --workspace
    # value must be shell-quoted or the argv would silently truncate at the space
    assert f"--workspace {ws!s}" not in service_unit
    exec_line = next(ln for ln in service_unit.splitlines() if ln.startswith("ExecStart="))
    assert "'/ws/acmé co/root dir'" in exec_line


def test_render_trigger_launchd_handles_workspace_with_spaces_and_non_ascii():
    ws = Path("/ws/acmé co/root dir")
    trig = _trigger(name="ingest", watch="inbox/ingest")
    xml = render_trigger_launchd(trig, ws, "cairn")
    doc = plistlib.loads(xml.encode("utf-8"))
    # ProgramArguments is a plist array — each element is its own string, no shell
    # word-splitting risk, so the raw (unquoted) path is the correct rendering
    assert doc["ProgramArguments"][-1] == str(ws)
    assert doc["WatchPaths"] == [str(ws / "inbox/ingest")]
