"""T14 / W3 close: ws-UUID host labels + migration, reconcile beat, ledger-version,
invariant audit, doctor cloud-sync lints.
"""

from __future__ import annotations

import plistlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cairn.kernel.errors import ConfigError
from cairn.kernel.fssafety import (
    check_watch_fs_safety,
    find_conflict_copies,
    hardlink_probe,
    is_under_cloud_sync,
)
from cairn.kernel.gckit import QUEUE_PIN_NAME, write_queue_pin
from cairn.kernel.proc import RunResult, RunnerBase
from cairn.kernel.queue_ledger import (
    LEDGER_VERSION,
    audit_ledger,
    check_ledger_version,
    read_ledger_version,
    stamp_ledger_version,
    write_pointer,
)
from cairn.kernel.trigger_host import (
    RECONCILE_BEAT_NAME,
    remove_trigger,
    sync_triggers,
    trigger_launchd_label,
    trigger_launchd_label_legacy,
)
from cairn.kernel.wsid import label_prefix_for, workspace_id, ws8

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

TRIGGERS_ONE = """\
handle-reply:
  pipeline: handle-reply
  watch: inbox/replies
"""


class FakeRunner(RunnerBase):
    def __init__(self):
        self.calls: list[dict] = []

    def spawn(self, argv, *, input=None, cwd=None):
        self.calls.append({"argv": list(argv), "input": input, "cwd": cwd})

        class _H:
            pid = 1

            def wait(self, timeout=None):
                return RunResult(returncode=0, stdout="", stderr="")

            def poll(self):
                return 0

            def terminate(self):
                return None

        return _H()


def _workspace(tmp_path: Path, triggers_yaml: str = TRIGGERS_ONE) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "pipelines").mkdir()
    (ws / "pipelines" / "handle-reply.yaml").write_text("nodes: {}\n", encoding="utf-8")
    (ws / "triggers.yaml").write_text(triggers_yaml, encoding="utf-8")
    (ws / "inbox" / "replies").mkdir(parents=True)
    return ws


# --------------------------------------------------------------------------- #
# 1. workspace UUID + scoped labels + migration
# --------------------------------------------------------------------------- #


def test_workspace_id_mints_once_and_caches(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    workspace_id(ws, _reset=True)
    a = workspace_id(ws)
    b = workspace_id(ws)
    assert a == b
    assert len(a) == 32
    assert all(c in "0123456789abcdef" for c in a)
    path = ws / ".cairn" / "workspace-id"
    assert path.is_file()
    # Re-read after cache clear still returns same on-disk id.
    workspace_id(ws, _reset=True)
    assert workspace_id(ws) == a


def test_label_scheme_embeds_ws8(tmp_path):
    ws = _workspace(tmp_path)
    wid = workspace_id(ws, _reset=True)
    assert trigger_launchd_label("handle-reply", wid) == f"io.cairn.{wid[:8]}.trigger.handle-reply"
    assert label_prefix_for(ws) == f"io.cairn.{wid[:8]}."


def test_sync_writes_ws_scoped_label(tmp_path):
    ws = _workspace(tmp_path)
    launchd_dir = tmp_path / "launchd"
    runner = FakeRunner()
    sync_triggers(ws, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)
    wid = workspace_id(ws)
    plist = launchd_dir / f"{trigger_launchd_label('handle-reply', wid)}.plist"
    assert plist.is_file()
    doc = plistlib.loads(plist.read_bytes())
    assert doc["Label"] == trigger_launchd_label("handle-reply", wid)
    assert "--workspace" in doc["ProgramArguments"]


def test_migration_replaces_own_legacy_unit_never_touches_other_ws(tmp_path):
    """Old-style unit for THIS ws is replaced; a different ws's unit is untouched."""
    ws_a = _workspace(tmp_path / "a")
    ws_b = tmp_path / "b" / "ws"
    ws_b.mkdir(parents=True)
    (ws_b / "pipelines").mkdir()
    (ws_b / "pipelines" / "handle-reply.yaml").write_text("nodes: {}\n", encoding="utf-8")
    (ws_b / "triggers.yaml").write_text(TRIGGERS_ONE, encoding="utf-8")
    (ws_b / "inbox" / "replies").mkdir(parents=True)

    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()

    # Seed legacy (unscoped) units for both workspaces sharing the same stem name.
    legacy_name = f"{trigger_launchd_label_legacy('handle-reply')}.plist"
    # Only ONE legacy stem exists in the shared dir — ownership is by --workspace argv.
    # Seed the unit for ws_a first (ours); also seed a distinct foreign unit for ws_b
    # under a different filename that still classifies as a trigger for handle-reply
    # of the other workspace (simulates the pre-W3 collision where only one could win;
    # with migration we attribute via argv).
    own_legacy = launchd_dir / legacy_name
    own_plist = {
        "Label": trigger_launchd_label_legacy("handle-reply"),
        "ProgramArguments": [
            "cairn",
            "trigger",
            "run",
            "handle-reply",
            "--workspace",
            str(ws_a.resolve()),
        ],
        "WatchPaths": [str(ws_a / "inbox/replies")],
    }
    own_legacy.write_bytes(plistlib.dumps(own_plist))

    # Other workspace's unit under a name that survives (new-style for B, or a
    # side-by-side planted file with B's workspace in argv at a unique path).
    other_label = f"io.cairn.deadbeef.trigger.handle-reply"
    other_plist_path = launchd_dir / f"{other_label}.plist"
    other_plist = {
        "Label": other_label,
        "ProgramArguments": [
            "cairn",
            "trigger",
            "run",
            "handle-reply",
            "--workspace",
            str(ws_b.resolve()),
        ],
        "WatchPaths": [str(ws_b / "inbox/replies")],
    }
    other_plist_path.write_bytes(plistlib.dumps(other_plist))
    other_bytes_before = other_plist_path.read_bytes()

    runner = FakeRunner()
    sync_triggers(ws_a, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)

    wid_a = workspace_id(ws_a)
    new_plist = launchd_dir / f"{trigger_launchd_label('handle-reply', wid_a)}.plist"
    assert new_plist.is_file(), "new-style unit for this workspace must be installed"
    # Legacy for THIS ws must be gone (migrated).
    assert not own_legacy.exists(), "own legacy unit must be replaced/removed"
    # Other workspace's unit untouched.
    assert other_plist_path.read_bytes() == other_bytes_before


def test_sync_idempotent_second_pass_no_runner_calls(tmp_path):
    ws = _workspace(tmp_path)
    launchd_dir = tmp_path / "launchd"
    runner = FakeRunner()
    sync_triggers(ws, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)
    runner.calls.clear()
    sync_triggers(ws, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)
    assert runner.calls == []


# --------------------------------------------------------------------------- #
# 2. Reconcile beat auto-install
# --------------------------------------------------------------------------- #


def test_sync_installs_reconcile_beat_idempotent_and_removes_when_empty(tmp_path):
    ws = _workspace(tmp_path)
    launchd_dir = tmp_path / "launchd"
    runner = FakeRunner()
    sync_triggers(ws, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)
    prefix = label_prefix_for(ws)
    beat = launchd_dir / f"{prefix}{RECONCILE_BEAT_NAME}.plist"
    assert beat.is_file()
    doc = plistlib.loads(beat.read_bytes())
    assert doc["ProgramArguments"][:3] == ["cairn", "factory", "reconcile"]
    assert "--workspace" in doc["ProgramArguments"]
    assert doc.get("RunAtLoad") is True

    # Second sync is idempotent for the beat.
    runner.calls.clear()
    sync_triggers(ws, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)
    assert runner.calls == []
    assert beat.is_file()

    # Remove last trigger → beat gone.
    (ws / "triggers.yaml").write_text("", encoding="utf-8")
    sync_triggers(ws, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)
    assert not beat.exists()


# --------------------------------------------------------------------------- #
# 3. Ledger-version marker
# --------------------------------------------------------------------------- #


def test_ledger_version_newer_refuses_equal_older_absent_proceed_and_stamp(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    # Absent → 0, proceeds, stamp writes current.
    assert read_ledger_version(watch) == 0
    check_ledger_version(watch)  # no raise
    stamp_ledger_version(watch)
    assert read_ledger_version(watch) == LEDGER_VERSION
    assert (watch / "ledger-version").read_text(encoding="utf-8").strip() == str(LEDGER_VERSION)

    # Equal → proceeds, stamp no-op (still equal).
    check_ledger_version(watch)
    stamp_ledger_version(watch)
    assert read_ledger_version(watch) == LEDGER_VERSION

    # Older → proceeds and bumps.
    (watch / "ledger-version").write_text("0\n", encoding="utf-8")
    check_ledger_version(watch)
    stamp_ledger_version(watch)
    assert read_ledger_version(watch) == LEDGER_VERSION

    # Newer → refuses loudly, no mutation of other state.
    (watch / "ledger-version").write_text(f"{LEDGER_VERSION + 99}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="ledger-version"):
        check_ledger_version(watch)


def test_drain_refuses_newer_ledger_version(tmp_path):
    from cairn.kernel.queue_drain import run_trigger

    ws = _workspace(tmp_path)
    watch = ws / "inbox" / "replies"
    (watch / "ledger-version").write_text(f"{LEDGER_VERSION + 5}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="ledger-version"):
        run_trigger(
            "handle-reply",
            ws,
            runner=FakeRunner(),
            cairn_bin="cairn",
            now=NOW,
        )


def test_sync_stamps_ledger_version(tmp_path):
    ws = _workspace(tmp_path)
    launchd_dir = tmp_path / "launchd"
    sync_triggers(
        ws, backend="launchd", runner=FakeRunner(), cairn_bin="cairn", launchd_dir=launchd_dir
    )
    assert read_ledger_version(ws / "inbox" / "replies") == LEDGER_VERSION


# --------------------------------------------------------------------------- #
# 4. Ledger invariant audit
# --------------------------------------------------------------------------- #


def test_audit_clean_ledger_reports_none(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    assert audit_ledger(watch) == []


def test_audit_pointer_without_item(tmp_path):
    watch = tmp_path / "inbox"
    claim = watch / ".claim"
    (claim / ".runs").mkdir(parents=True)
    write_pointer(claim / ".runs" / "orphan.json", run_dir="/runs/x")
    issues = audit_ledger(watch)
    assert any("pointer without item" in i for i in issues)


def test_audit_item_without_pointer(tmp_path):
    watch = tmp_path / "inbox"
    claim = watch / ".claim"
    claim.mkdir(parents=True)
    (claim / "lone.json").write_text("{}", encoding="utf-8")
    issues = audit_ledger(watch)
    assert any("item without pointer" in i for i in issues)


def test_audit_claim_without_reservation_for_strict_name(tmp_path):
    watch = tmp_path / "inbox"
    claim = watch / ".claim"
    claim.mkdir(parents=True)
    name = "p1-jira-abc-r10.json"
    (claim / name).write_text("{}", encoding="utf-8")
    write_pointer(claim / ".runs" / name, run_dir="/runs/x")
    issues = audit_ledger(watch)
    assert any("claim without reservation" in i for i in issues)


def test_audit_identity_in_two_live_states(tmp_path):
    watch = tmp_path / "inbox"
    name_a = "p1-jira-abc-r10.json"
    name_b = "p2-jira-abc-r11.json"  # same identity jira-abc
    for lane, name in ((".claim", name_a), (".waiting", name_b)):
        d = watch / lane
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("{}", encoding="utf-8")
        write_pointer(d / ".runs" / name, run_dir="/runs/x")
    # reservations for both so only the two-state issue fires for identity
    (watch / ".claim" / ".ids").mkdir(parents=True)
    (watch / ".claim" / ".ids" / "jira-abc").write_text("", encoding="utf-8")
    issues = audit_ledger(watch)
    assert any("identity in two live states" in i for i in issues)


def test_audit_terminal_run_still_pinned(tmp_path):
    watch = tmp_path / "inbox"
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    write_queue_pin(run_dir, trigger="t", item="x.json", pinned_at="2026-01-01T00:00:00Z")
    failed = watch / ".failed"
    failed.mkdir(parents=True)
    (failed / "x.json").write_text("{}", encoding="utf-8")
    write_pointer(failed / ".runs" / "x.json", run_dir=str(run_dir), outcome="failed", exit_code=1)
    issues = audit_ledger(watch)
    assert any("terminal-ledger run still pinned" in i for i in issues)
    assert (run_dir / QUEUE_PIN_NAME).is_file()  # audit never mutates


# --------------------------------------------------------------------------- #
# 5. Doctor / FS safety lints
# --------------------------------------------------------------------------- #


def test_cloud_sync_detection_with_injected_home(tmp_path):
    home = tmp_path / "home"
    dropbox = home / "Dropbox" / "cairn-inbox"
    dropbox.mkdir(parents=True)
    reason = is_under_cloud_sync(dropbox, home=home)
    assert reason is not None
    assert "Dropbox" in reason

    icloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "ws"
    icloud.mkdir(parents=True)
    assert is_under_cloud_sync(icloud, home=home) is not None

    local = tmp_path / "local-inbox"
    local.mkdir()
    assert is_under_cloud_sync(local, home=home) is None


def test_cloud_sync_extra_roots_injection(tmp_path):
    root = tmp_path / "fake-cloud"
    watch = root / "inbox"
    watch.mkdir(parents=True)
    assert is_under_cloud_sync(watch, extra_roots=[root]) is not None


def test_hardlink_probe_passes_on_local_tmp(tmp_path):
    d = tmp_path / "hl"
    d.mkdir()
    assert hardlink_probe(d) is True


def test_conflict_copy_detection(tmp_path):
    watch = tmp_path / "inbox"
    claim = watch / ".claim"
    claim.mkdir(parents=True)
    (claim / "item 2.json").write_text("{}", encoding="utf-8")
    (claim / "foo.icloud").write_text("", encoding="utf-8")
    (claim / "bar.conflict").write_text("", encoding="utf-8")
    hits = find_conflict_copies(watch)
    assert any("2.json" in h for h in hits)
    assert any(".icloud" in h for h in hits)
    assert any("conflict" in h for h in hits)


def test_check_watch_fs_safety_surfaces_errors(tmp_path):
    home = tmp_path / "home"
    watch = home / "Dropbox" / "inbox"
    watch.mkdir(parents=True)
    findings = check_watch_fs_safety(watch, home=home)
    assert any(f.level == "error" and "cloud-sync" in f.message for f in findings)


def test_cli_unsafe_synced_fs_override_is_documented_on_parser():
    from cairn.cli import _build_parser

    p = _build_parser()
    # trigger + factory both expose the override.
    ns = p.parse_args(["trigger", "sync", "--unsafe-synced-fs"])
    assert ns.unsafe_synced_fs is True
    ns2 = p.parse_args(["factory", "reconcile", "--unsafe-synced-fs"])
    assert ns2.unsafe_synced_fs is True
