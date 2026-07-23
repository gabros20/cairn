"""Render-only backends: launchd WatchPaths plist, systemd .path + .service unit pair.

Pure string generation, fully offline — these never touch the host watcher
(docs/TRIGGERS-PLAN.md §3).
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from cairn.kernel.errors import ConfigError
from cairn.kernel.triggerkit import (
    Trigger,
    render_trigger_launchd,
    render_trigger_systemd,
    trigger_launchd_label,
    trigger_systemd_unit_names,
)

WS = Path("/ws/acme")
# Fixed workspace UUID for offline render tests (no mint of .cairn/workspace-id).
WID = "aabbccdd11223344556677889900aabb"
WS8 = WID[:8]


def _trigger(name="handle-reply", pipeline="handle-reply", watch="inbox/replies", **kw):
    return Trigger(name=name, pipeline=pipeline, watch=watch, **kw)


# --- label / unit-name helpers ------------------------------------------------


def test_trigger_launchd_label_is_ws_scoped_under_trigger():
    assert (
        trigger_launchd_label("handle-reply", WID)
        == f"io.cairn.{WS8}.trigger.handle-reply"
    )


def test_trigger_systemd_unit_names_are_ws_scoped_under_trigger():
    assert trigger_systemd_unit_names("handle-reply", WID) == (
        f"cairn-{WS8}-trigger-handle-reply.path",
        f"cairn-{WS8}-trigger-handle-reply.service",
    )


def test_trigger_and_schedule_namespaces_never_collide_on_a_shared_name():
    # a schedule and a trigger named identically must never produce the same unit/label
    from cairn.kernel.schedkit import launchd_label, systemd_unit_names

    prefix = f"io.cairn.{WS8}."
    assert trigger_launchd_label("weekly", WID) != launchd_label("weekly", prefix)
    trig_path, trig_service = trigger_systemd_unit_names("weekly", WID)
    sched_service, sched_timer = systemd_unit_names("weekly", ws_id=WID)
    assert trig_path not in (sched_service, sched_timer)
    assert trig_service not in (sched_service, sched_timer)


# --- launchd --------------------------------------------------------------


def test_render_trigger_launchd_is_valid_plist_with_watchpaths_and_argv():
    xml = render_trigger_launchd(_trigger(), WS, "cairn", ws_id=WID)
    doc = plistlib.loads(xml.encode("utf-8"))
    assert doc["Label"] == f"io.cairn.{WS8}.trigger.handle-reply"
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
    xml = render_trigger_launchd(trig, WS, "cairn", ws_id=WID)
    doc = plistlib.loads(xml.encode("utf-8"))
    watch = Path(doc["WatchPaths"][0])
    assert watch.is_absolute()
    assert watch == WS / "inbox/ingest"


# --- systemd ----------------------------------------------------------------


def test_render_trigger_systemd_path_and_service():
    path_unit, service_unit = render_trigger_systemd(_trigger(), WS, "cairn", ws_id=WID)

    assert "[Path]" in path_unit
    assert f"DirectoryNotEmpty={WS / 'inbox/replies'}" in path_unit
    assert f"Unit=cairn-{WS8}-trigger-handle-reply.service" in path_unit
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
    _, service_unit = render_trigger_systemd(trig, WS, "cairn", ws_id=WID)
    assert "ExecStart=cairn trigger run ingest --workspace /ws/acme" in service_unit
    # editing triggers.yaml (pipeline/param/glob/on_done) must change behavior without
    # re-sync — none of those fields ever leak into the rendered argv
    assert "handle-reply" not in service_unit.split("ExecStart=")[1]
    assert "event" not in service_unit.split("ExecStart=")[1]


def test_render_trigger_systemd_quotes_workspace_with_spaces_and_non_ascii():
    ws = Path("/ws/acmé co/root dir")
    trig = _trigger(name="ingest", watch="inbox/ingest")
    path_unit, service_unit = render_trigger_systemd(trig, ws, "cairn", ws_id=WID)

    # DirectoryNotEmpty= is a single unsplit value — no quoting needed or added
    assert f"DirectoryNotEmpty={ws / 'inbox/ingest'}" in path_unit
    # WorkingDirectory= is likewise a single unsplit value (unlike word-split
    # ExecStart=) — emitted unquoted, and must be pinned for the space-bearing path too
    assert f"WorkingDirectory={ws}" in service_unit
    # ExecStart= IS word-split by systemd, so the space-and-accent-bearing --workspace
    # value must be shell-quoted or the argv would silently truncate at the space
    assert f"--workspace {ws!s}" not in service_unit
    exec_line = next(ln for ln in service_unit.splitlines() if ln.startswith("ExecStart="))
    assert "'/ws/acmé co/root dir'" in exec_line


def test_render_trigger_launchd_handles_workspace_with_spaces_and_non_ascii():
    ws = Path("/ws/acmé co/root dir")
    trig = _trigger(name="ingest", watch="inbox/ingest")
    xml = render_trigger_launchd(trig, ws, "cairn", ws_id=WID)
    doc = plistlib.loads(xml.encode("utf-8"))
    # ProgramArguments is a plist array — each element is its own string, no shell
    # word-splitting risk, so the raw (unquoted) path is the correct rendering
    assert doc["ProgramArguments"][-1] == str(ws)
    assert doc["WatchPaths"] == [str(ws / "inbox/ingest")]


# --- render-time control-character belt (review-T2-quality-r1.md Finding 1) ----------
#
# load_triggers/_parse_trigger validates trigger.name (charset) and watch (control
# chars) before a Trigger is ever constructed via the normal load path (see
# test_triggerkit_load.py). These tests prove the SECOND, independent layer: T1's
# Trigger dataclass is a public type nothing stops constructing directly (bypassing
# load), and workspace_dir/cairn_bin are CLI arguments load-time validation can never
# see at all — render_trigger_systemd/_launchd must reject a control character in any
# interpolated value themselves, before formatting any unit text, or the reviewer's
# exact repro (a value containing "\n[Service]") reaches the returned string.


def test_render_trigger_systemd_rejects_injection_payload_in_name_bypassing_load():
    evil_name = "evil\n[Service]\nExecStart=/bin/touch /tmp/pwned\n#"
    trig = Trigger(name=evil_name, pipeline="p", watch="inbox")
    with pytest.raises(ConfigError, match="trigger name"):
        render_trigger_systemd(trig, WS, "cairn", ws_id=WID)


def test_render_trigger_systemd_rejects_injection_payload_in_watch_bypassing_load():
    evil_watch = "inbox\n[Service]\nExecStart=/bin/touch /tmp/pwned3\n#"
    trig = Trigger(name="ok", pipeline="p", watch=evil_watch)
    with pytest.raises(ConfigError, match="watch directory"):
        render_trigger_systemd(trig, WS, "cairn", ws_id=WID)


def test_render_trigger_systemd_rejects_injection_payload_in_workspace_dir():
    # workspace_dir comes from the CLI, never through triggers.yaml — load-time
    # validation structurally cannot cover this vector; only the render-side belt does.
    trig = _trigger()
    evil_ws = Path("/ws/acme\n[Service]\nExecStart=/bin/touch /tmp/pwned\n#")
    with pytest.raises(ConfigError, match="workspace_dir"):
        render_trigger_systemd(trig, evil_ws, "cairn", ws_id=WID)


def test_render_trigger_systemd_rejects_injection_payload_in_cairn_bin():
    trig = _trigger()
    with pytest.raises(ConfigError, match="cairn_bin"):
        render_trigger_systemd(
            trig, WS, "cairn\n[Service]\nExecStart=/bin/touch /tmp/pwned\n#", ws_id=WID
        )


def test_render_trigger_systemd_injection_attempts_never_produce_rendered_text():
    # Positive proof, not just "it raises": the exception fires before path_unit/
    # service_unit are ever built, so no injected "[Service]" text is ever returned to a
    # caller for any of the vectors above.
    evil_name = "evil\n[Service]\nExecStart=/bin/touch /tmp/pwned\n#"
    trig = Trigger(name=evil_name, pipeline="p", watch="inbox")
    try:
        render_trigger_systemd(trig, WS, "cairn", ws_id=WID)
        raised = False
    except ConfigError as exc:
        raised = True
        assert "[Service]\nExecStart=/bin/touch" not in str(exc)
    assert raised


def test_render_trigger_launchd_rejects_injection_payload_in_workspace_dir():
    # Symmetry of guarantees per the brief: plistlib already XML-escapes a control
    # character safely, but the belt is explicit here rather than left implicit in a
    # third-party serializer's behavior.
    trig = _trigger()
    evil_ws = Path("/ws/acme\n[Service]\nExecStart=/bin/touch /tmp/pwned\n#")
    with pytest.raises(ConfigError, match="workspace_dir"):
        render_trigger_launchd(trig, evil_ws, "cairn", ws_id=WID)


def test_render_trigger_launchd_rejects_injection_payload_in_name_bypassing_load():
    evil_name = "evil\n[Service]\nExecStart=/bin/touch /tmp/pwned\n#"
    trig = Trigger(name=evil_name, pipeline="p", watch="inbox")
    with pytest.raises(ConfigError, match="trigger name"):
        render_trigger_launchd(trig, WS, "cairn", ws_id=WID)


def test_trigger_argv_appends_lane_when_set():
    from cairn.kernel.trigger_host import _trigger_argv

    trig = _trigger(lane="dark")
    argv = _trigger_argv(trig, WS, "cairn")
    assert argv == [
        "cairn", "trigger", "run", "handle-reply",
        "--workspace", str(WS),
        "--lane", "dark",
    ]


def test_trigger_argv_omits_lane_when_absent():
    from cairn.kernel.trigger_host import _trigger_argv

    argv = _trigger_argv(_trigger(), WS, "cairn")
    assert "--lane" not in argv
