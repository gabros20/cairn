"""install / uninstall / list_installed / run_schedule — the effectful verbs.

Every side effect is dependency-injected (a fake Runner + tmp target dirs); no test ever
touches the real crontab, launchctl, systemctl, or ~/Library.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from cairn.kernel.errors import ConfigError
from cairn.kernel.schedkit import (
    InstalledEntry,
    RunResult,
    Schedule,
    diff_schedules,
    install,
    list_installed,
    run_schedule,
    uninstall,
)

WS = Path("/ws/acme")


def _sched(cron="0 3 * * 1", name="weekly", run=("run", "brease-rebrand", "--headless")):
    return Schedule(name=name, cron=cron, run=tuple(run))


class FakeRunner:
    """Records every argv/input/cwd; returns canned results keyed by the first two argv tokens."""

    def __init__(self, canned=None):
        self.calls: list[dict] = []
        self._canned = canned or {}

    def run(self, argv, *, input=None, cwd=None) -> RunResult:
        self.calls.append({"argv": list(argv), "input": input, "cwd": cwd})
        key = tuple(argv[:2])
        return self._canned.get(key, RunResult(returncode=0, stdout="", stderr=""))


# --- cron install/uninstall -------------------------------------------------


def test_install_cron_pipes_merged_crontab_preserving_foreign_entries():
    runner = FakeRunner({("crontab", "-l"): RunResult(0, "0 0 * * * /usr/bin/backup\n")})
    install({"weekly": _sched()}, "cron", workspace_dir=WS, runner=runner)
    write = next(c for c in runner.calls if c["argv"] == ["crontab", "-"])
    assert "/usr/bin/backup" in write["input"]
    assert "schedule run weekly" in write["input"]


def test_install_cron_tolerates_no_existing_crontab():
    # `crontab -l` exits non-zero when the user has no crontab — treated as empty, not fatal
    runner = FakeRunner({("crontab", "-l"): RunResult(1, "", "no crontab for user")})
    install({"weekly": _sched()}, "cron", workspace_dir=WS, runner=runner)
    write = next(c for c in runner.calls if c["argv"] == ["crontab", "-"])
    assert "schedule run weekly" in write["input"]


def test_uninstall_cron_strips_only_managed_block():
    installed_input = {}

    class R(FakeRunner):
        def run(self, argv, *, input=None, cwd=None):
            if argv == ["crontab", "-"]:
                installed_input["text"] = input
            return super().run(argv, input=input, cwd=cwd)

    runner = R({("crontab", "-l"): RunResult(0, "")})
    install({"weekly": _sched()}, "cron", workspace_dir=WS, runner=runner)
    with_block = installed_input["text"]

    runner2 = R({("crontab", "-l"): RunResult(0, "0 0 * * * /usr/bin/backup\n" + with_block)})
    uninstall("cron", workspace_dir=WS, runner=runner2)
    assert "schedule run weekly" not in installed_input["text"]
    assert "/usr/bin/backup" in installed_input["text"]


# --- launchd (writes to an injected dir, never ~/Library) -------------------


def test_install_launchd_writes_plist_and_loads_via_runner(tmp_path):
    runner = FakeRunner()
    install({"weekly": _sched()}, "launchd", workspace_dir=WS, runner=runner, launchd_dir=tmp_path)
    plist = tmp_path / "io.cairn.weekly.plist"
    assert plist.is_file()
    doc = plistlib.loads(plist.read_bytes())
    assert doc["ProgramArguments"] == ["cairn", "schedule", "run", "weekly"]
    assert any(c["argv"][:1] == ["launchctl"] for c in runner.calls)


def test_uninstall_launchd_removes_plists_in_dir(tmp_path):
    runner = FakeRunner()
    install({"weekly": _sched()}, "launchd", workspace_dir=WS, runner=runner, launchd_dir=tmp_path)
    assert (tmp_path / "io.cairn.weekly.plist").is_file()
    uninstall("launchd", workspace_dir=WS, runner=runner, launchd_dir=tmp_path)
    assert not (tmp_path / "io.cairn.weekly.plist").exists()


# --- systemd ----------------------------------------------------------------


def test_install_systemd_writes_units_and_enables_timer(tmp_path):
    runner = FakeRunner()
    install({"weekly": _sched()}, "systemd", workspace_dir=WS, runner=runner, systemd_dir=tmp_path)
    assert (tmp_path / "cairn-weekly.service").is_file()
    assert (tmp_path / "cairn-weekly.timer").is_file()
    assert any("enable" in c["argv"] for c in runner.calls)


# --- list + diff ------------------------------------------------------------


def test_list_installed_cron_reads_back_managed_block():
    runner = FakeRunner()
    install({"weekly": _sched(), "nightly": _sched(cron="30 2 * * *", name="nightly")},
            "cron", workspace_dir=WS, runner=runner)
    written = next(c for c in runner.calls if c["argv"] == ["crontab", "-"])["input"]
    reader = FakeRunner({("crontab", "-l"): RunResult(0, written)})
    installed = list_installed("cron", runner=reader)
    assert set(installed) == {"weekly", "nightly"}
    assert installed["weekly"].schedule == "0 3 * * 1"


def test_diff_reports_added_removed_changed_unchanged():
    declared = {
        "weekly": _sched(cron="0 3 * * 1"),
        "nightly": _sched(cron="30 2 * * *", name="nightly"),
        "moved": _sched(cron="0 6 * * *", name="moved"),
    }
    installed = {
        "weekly": InstalledEntry("weekly", "0 3 * * 1"),   # unchanged
        "moved": InstalledEntry("moved", "0 5 * * *"),     # cron changed
        "stale": InstalledEntry("stale", "0 0 * * *"),     # removed (not declared)
    }
    diff = diff_schedules(declared, installed)
    assert diff.added == ("nightly",)
    assert diff.removed == ("stale",)
    assert diff.changed == ("moved",)
    assert diff.unchanged == ("weekly",)


# --- run_schedule -----------------------------------------------------------


def test_run_schedule_composes_argv_and_returns_exit_code():
    runner = FakeRunner({("cairn", "run"): RunResult(0, "")})
    code = run_schedule({"weekly": _sched()}, "weekly", workspace_dir=WS, runner=runner)
    assert code == 0
    call = runner.calls[-1]
    assert call["argv"] == ["cairn", "run", "brease-rebrand", "--headless"]
    assert call["cwd"] == WS


def test_run_schedule_propagates_nonzero_exit():
    runner = FakeRunner({("cairn", "run"): RunResult(4, "", "executor crashed")})
    code = run_schedule({"weekly": _sched()}, "weekly", workspace_dir=WS, runner=runner)
    assert code == 4


def test_run_schedule_unknown_name_is_config_error():
    with pytest.raises(ConfigError, match="no schedule named 'ghost'"):
        run_schedule({"weekly": _sched()}, "ghost", workspace_dir=WS, runner=FakeRunner())
