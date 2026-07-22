"""sync_triggers idempotency: a repeated sync of an unchanged triggers.yaml issues ZERO
host-watcher calls (not merely unchanged bytes on disk), and pruning touches only files
this module can actually attribute to itself — an unmanaged or schedule-owned file
sitting at a now-stale managed stem is never deleted (TRIGGERS-PLAN.md §3, requirement
5, spec finding F1 / addendum 2).
"""

from __future__ import annotations

from pathlib import Path

from cairn.kernel.proc import RunResult, RunnerBase
from cairn.kernel.triggerkit import sync_triggers, trigger_launchd_label, trigger_systemd_unit_names

TRIGGERS_ONE = """\
handle-reply:
  pipeline: handle-reply
  watch: inbox/replies
"""

TRIGGERS_TWO = """\
handle-reply:
  pipeline: handle-reply
  watch: inbox/replies
other:
  pipeline: other
  watch: inbox/other
"""

_SCHEDULE_OWNED_SERVICE = (
    "[Unit]\nDescription=cairn schedule: trigger-X\n\n[Service]\nType=oneshot\n"
    "WorkingDirectory=/ws/acme\nExecStart=cairn schedule run trigger-X\n"
).encode("utf-8")


class _CannedHandle:
    """Immediate ProcessHandle for test fakes — pid fixed, wait returns a canned RunResult."""

    def __init__(self, result: RunResult, pid: int = 1):
        self._result = result
        self._pid = pid

    @property
    def pid(self) -> int:
        return self._pid

    def wait(self, timeout=None) -> RunResult:
        return self._result

    def poll(self) -> int | None:
        return self._result.returncode

    def terminate(self) -> None:
        return None


class FakeRunner(RunnerBase):
    """Records every argv/input/cwd; returns canned results keyed by the first two argv tokens."""

    def __init__(self, canned=None):
        self.calls: list[dict] = []
        self._canned = canned or {}

    def spawn(self, argv, *, input=None, cwd=None) -> _CannedHandle:
        self.calls.append({"argv": list(argv), "input": input, "cwd": cwd})
        key = tuple(argv[:2])
        result = self._canned.get(key, RunResult(returncode=0, stdout="", stderr=""))
        return _CannedHandle(result)


def _workspace(tmp_path: Path, triggers_yaml: str, *, pipelines=("handle-reply",)) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "pipelines").mkdir()
    for p in pipelines:
        (ws / "pipelines" / f"{p}.yaml").write_text("nodes: {}\n", encoding="utf-8")
    (ws / "triggers.yaml").write_text(triggers_yaml, encoding="utf-8")
    return ws


# --- double sync = true no-op --------------------------------------------------


def test_double_sync_launchd_is_a_true_noop_second_time(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    launchd_dir = tmp_path / "launchd"
    runner = FakeRunner()
    sync_triggers(ws, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)
    plist = launchd_dir / f"{trigger_launchd_label('handle-reply')}.plist"
    before = plist.read_bytes()

    runner.calls.clear()
    touched = sync_triggers(ws, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)

    assert plist.read_bytes() == before  # identical bytes, not just "no error"
    assert runner.calls == []  # zero unload/load calls — a true no-op, not just unwritten bytes
    assert touched == [plist.name]  # still reported as installed, even though nothing changed


def test_double_sync_systemd_is_a_true_noop_second_time(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    systemd_dir = tmp_path / "systemd"
    runner = FakeRunner()
    sync_triggers(ws, backend="systemd", runner=runner, cairn_bin="cairn", systemd_dir=systemd_dir)
    path_name, service_name = trigger_systemd_unit_names("handle-reply")
    before_path = (systemd_dir / path_name).read_bytes()
    before_service = (systemd_dir / service_name).read_bytes()

    runner.calls.clear()
    touched = sync_triggers(ws, backend="systemd", runner=runner, cairn_bin="cairn", systemd_dir=systemd_dir)

    assert (systemd_dir / path_name).read_bytes() == before_path
    assert (systemd_dir / service_name).read_bytes() == before_service
    assert runner.calls == []  # zero daemon-reload/enable calls
    assert set(touched) == {path_name, service_name}


# --- prune on removal -----------------------------------------------------------


def test_sync_after_removing_a_trigger_prunes_only_its_own_managed_file(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_TWO, pipelines=("handle-reply", "other"))
    launchd_dir = tmp_path / "launchd"
    runner = FakeRunner()
    sync_triggers(ws, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)
    kept_plist = launchd_dir / f"{trigger_launchd_label('handle-reply')}.plist"
    removed_plist = launchd_dir / f"{trigger_launchd_label('other')}.plist"
    assert kept_plist.is_file() and removed_plist.is_file()

    # an unmanaged file sitting in the same directory must never be touched
    foreign = launchd_dir / "io.somebody.else.plist"
    foreign.write_text("not ours", encoding="utf-8")

    (ws / "triggers.yaml").write_text(TRIGGERS_ONE, encoding="utf-8")  # "other" no longer declared
    runner.calls.clear()
    touched = sync_triggers(ws, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)

    assert kept_plist.is_file()  # still-declared trigger's file survives, untouched by the prune
    assert not removed_plist.exists()  # the removed trigger's own file is gone
    assert foreign.is_file() and foreign.read_text(encoding="utf-8") == "not ours"
    assert removed_plist.name in touched
    assert any(
        c["argv"][:2] == ["launchctl", "unload"] and c["argv"][2] == str(removed_plist)
        for c in runner.calls
    )
    # the kept trigger triggered no reload calls of its own — it was already current
    assert not any(c["argv"][2] == str(kept_plist) for c in runner.calls if len(c["argv"]) > 2)


def test_sync_never_prunes_a_schedule_owned_file_at_a_stale_managed_stem(tmp_path):
    # A schedule "trigger-X" would render cairn-trigger-X.service — the exact stem a
    # trigger "X" would use too. "X" is never declared here, so a naive glob-and-prune
    # (name-only, no content check) would delete the schedule's LIVE unit file. Sync
    # must recognize it isn't ours (spec finding F1 / addendum 2) and leave it alone.
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    foreign_service = systemd_dir / "cairn-trigger-X.service"
    foreign_service.write_bytes(_SCHEDULE_OWNED_SERVICE)

    runner = FakeRunner()
    sync_triggers(ws, backend="systemd", runner=runner, cairn_bin="cairn", systemd_dir=systemd_dir)

    assert foreign_service.read_bytes() == _SCHEDULE_OWNED_SERVICE
    assert all("cairn-trigger-X.service" not in c["argv"] for c in runner.calls)
