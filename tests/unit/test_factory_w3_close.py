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
    """Own legacy unit migrated; foreign LEGACY-style unit for another ws untouched.

    Plants the foreign unit as ``io.cairn.trigger.<other-name>.plist`` (legacy glob)
    with a different workspace's ``--workspace`` argv so ``_iter_launchd_trigger_plists``
    actually scans it and ``_same_workspace`` ownership attribution is exercised (I2).
    """
    ws_a = _workspace(tmp_path / "a")
    ws_b = tmp_path / "b" / "ws"
    ws_b.mkdir(parents=True)
    (ws_b / "pipelines").mkdir()
    (ws_b / "pipelines" / "handle-reply.yaml").write_text("nodes: {}\n", encoding="utf-8")
    (ws_b / "triggers.yaml").write_text(TRIGGERS_ONE, encoding="utf-8")
    (ws_b / "inbox" / "replies").mkdir(parents=True)

    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()

    # Own legacy unit for handle-reply (this workspace) — will be migrated.
    own_legacy = launchd_dir / f"{trigger_launchd_label_legacy('handle-reply')}.plist"
    own_legacy.write_bytes(
        plistlib.dumps(
            {
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
        )
    )

    # Foreign LEGACY-style unit for a DIFFERENT trigger name + DIFFERENT workspace.
    # Lands in the io.cairn.trigger.*.plist glob; ownership via --workspace must
    # leave it alone when ws_a syncs.
    foreign_legacy = launchd_dir / f"{trigger_launchd_label_legacy('foreign-job')}.plist"
    foreign_legacy.write_bytes(
        plistlib.dumps(
            {
                "Label": trigger_launchd_label_legacy("foreign-job"),
                "ProgramArguments": [
                    "cairn",
                    "trigger",
                    "run",
                    "foreign-job",
                    "--workspace",
                    str(ws_b.resolve()),
                ],
                "WatchPaths": [str(ws_b / "inbox/other")],
            }
        )
    )
    foreign_bytes_before = foreign_legacy.read_bytes()

    runner = FakeRunner()
    sync_triggers(ws_a, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)

    wid_a = workspace_id(ws_a)
    new_plist = launchd_dir / f"{trigger_launchd_label('handle-reply', wid_a)}.plist"
    assert new_plist.is_file(), "new-style unit for this workspace must be installed"
    assert not own_legacy.exists(), "own legacy unit must be replaced/removed"
    assert foreign_legacy.is_file(), "foreign legacy unit must still exist"
    assert foreign_legacy.read_bytes() == foreign_bytes_before, (
        "foreign workspace's legacy unit must be byte-for-byte untouched"
    )


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


def test_cloud_sync_does_not_false_refuse_unanchored_dropbox_name(tmp_path):
    """M1: a local dir literally named Dropbox outside $HOME is not cloud-sync."""
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "mnt" / "external" / "Dropbox" / "scratch" / "watch"
    elsewhere.mkdir(parents=True)
    assert is_under_cloud_sync(elsewhere, home=home) is None


# --------------------------------------------------------------------------- #
# Fix wave r1 — C1 beat ownership, C2 registry, I3 exclusive mint, I1 liveness
# --------------------------------------------------------------------------- #


def test_reconcile_beat_removal_only_touches_own_workspace(tmp_path):
    """C1: two workspaces' beats coexist; emptying ws_a removes ONLY its beat."""
    ws_a = _workspace(tmp_path / "a")
    ws_b = _workspace(tmp_path / "b")
    launchd_dir = tmp_path / "launchd"
    runner = FakeRunner()

    sync_triggers(ws_a, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)
    sync_triggers(ws_b, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)

    beat_a = launchd_dir / f"{label_prefix_for(ws_a)}{RECONCILE_BEAT_NAME}.plist"
    beat_b = launchd_dir / f"{label_prefix_for(ws_b)}{RECONCILE_BEAT_NAME}.plist"
    assert beat_a.is_file() and beat_b.is_file()
    assert beat_a.resolve() != beat_b.resolve()
    b_bytes = beat_b.read_bytes()

    # Empty ws_a's triggers → its beat drops; ws_b's beat must survive.
    (ws_a / "triggers.yaml").write_text("", encoding="utf-8")
    sync_triggers(ws_a, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)
    assert not beat_a.exists(), "ws_a beat must be removed when last trigger is gone"
    assert beat_b.is_file(), "ws_b beat must survive"
    assert beat_b.read_bytes() == b_bytes


def test_duplicate_uuid_on_copied_workspace_is_reminted(tmp_path):
    """C2: cp -r of a workspace re-mints on first workspace_id(); labels differ."""
    import json

    import cairn.kernel.wsid as wsid

    reg = tmp_path / "reg.json"
    src = tmp_path / "src"
    src.mkdir()
    workspace_id(src, _reset=True, registry_path=reg)
    src_id = workspace_id(src, registry_path=reg)
    assert (src / ".cairn" / "workspace-id").is_file()

    # Simulate cp -r: same ws-id file, different path.
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / ".cairn").mkdir()
    (copy / ".cairn" / "workspace-id").write_text(src_id + "\n", encoding="utf-8")
    wsid._CACHE.clear()

    copy_id = workspace_id(copy, registry_path=reg)
    assert copy_id != src_id, "copy must re-mint a distinct UUID"
    assert (copy / ".cairn" / "workspace-id").read_text(encoding="utf-8").strip() == copy_id
    # Original unchanged.
    wsid._CACHE.clear()
    assert workspace_id(src, registry_path=reg) == src_id
    # Registry has both.
    data = json.loads(reg.read_text(encoding="utf-8"))
    assert src_id in data and copy_id in data
    assert data[src_id] == str(src.resolve())
    assert data[copy_id] == str(copy.resolve())
    # Labels differ.
    assert trigger_launchd_label("t", src_id) != trigger_launchd_label("t", copy_id)


def test_concurrent_first_mint_converges_on_one_uuid(tmp_path):
    """I3: exclusive_create mint — loser of O_EXCL race adopts the winner's UUID."""
    from cairn.kernel.durafs import exclusive_create
    from cairn.kernel.wsid import _mint_exclusive

    path = tmp_path / ".cairn" / "workspace-id"
    path.parent.mkdir(parents=True)
    winner = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert exclusive_create(path, winner + "\n") is True
    # Simulated loser: exclusive_create fails (name taken) → re-reads winner.
    got = _mint_exclusive(path)
    assert got == winner

    # End-to-end: workspace_id on a pre-created id file adopts it (no second mint).
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".cairn").mkdir()
    (ws / ".cairn" / "workspace-id").write_text(winner + "\n", encoding="utf-8")
    import cairn.kernel.wsid as wsid

    wsid._CACHE.clear()
    assert workspace_id(ws, registry_path=tmp_path / "reg.json") == winner


def test_audit_in_flight_claim_with_live_lease_is_not_a_violation(tmp_path):
    """I1: claim without pointer + live drain_pid → audit silent."""
    import os

    from cairn.kernel.queue_ledger import write_lease

    watch = tmp_path / "inbox"
    claim = watch / ".claim"
    claim.mkdir(parents=True)
    name = "in-flight.json"
    (claim / name).write_text("{}", encoding="utf-8")
    # No pointer yet — the claim→spawn window.
    write_lease(
        watch,
        name,
        drain_pid=os.getpid(),  # THIS process is live
        child_pid=None,
        boot_id="boot-test",
        claimed_at=1.0,
        ttl_s=3600,
    )
    issues = audit_ledger(watch, current_boot_id="boot-test")
    assert not any("item without pointer" in i for i in issues)


def test_audit_orphaned_claim_without_pointer_is_reported(tmp_path):
    """I1: claim without pointer + dead/absent owner → real violation."""
    watch = tmp_path / "inbox"
    claim = watch / ".claim"
    claim.mkdir(parents=True)
    name = "orphan.json"
    (claim / name).write_text("{}", encoding="utf-8")
    # No lease at all → dead/absent owner.
    issues = audit_ledger(watch, current_boot_id="boot-test")
    assert any("item without pointer" in i for i in issues)
