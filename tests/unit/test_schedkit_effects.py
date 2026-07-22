"""install / uninstall / list_installed / run_schedule — the effectful verbs.

Every side effect is dependency-injected (a fake Runner + tmp target dirs); no test ever
touches the real crontab, launchctl, systemctl, or ~/Library.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from cairn.kernel.errors import ConfigError
from cairn.kernel.proc import RunnerBase
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
        def spawn(self, argv, *, input=None, cwd=None):
            if argv == ["crontab", "-"]:
                installed_input["text"] = input
            return super().spawn(argv, input=input, cwd=cwd)

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


def _install_list_diff(schedules):
    """install → read back the piped crontab → list → diff against the same declaration."""
    runner = FakeRunner()
    install(schedules, "cron", workspace_dir=WS, runner=runner)
    written = next(c for c in runner.calls if c["argv"] == ["crontab", "-"])["input"]
    reader = FakeRunner({("crontab", "-l"): RunResult(0, written)})
    installed = list_installed("cron", runner=reader)
    return installed, diff_schedules(schedules, installed)


def test_macro_schedule_reads_back_unchanged_not_garbled():
    # @daily has 1 token, not 5 — positional slicing would garble it and report `changed` forever.
    schedules = {"nightly": _sched(cron="@daily", name="nightly")}
    installed, diff = _install_list_diff(schedules)
    assert installed["nightly"].schedule == "@daily"
    assert diff.unchanged == ("nightly",)
    assert diff.changed == ()


def test_stepped_list_schedule_reads_back_unchanged():
    schedules = {"quarterly": _sched(cron="*/15 * * * *", name="quarterly")}
    installed, diff = _install_list_diff(schedules)
    assert installed["quarterly"].schedule == "*/15 * * * *"
    assert diff.unchanged == ("quarterly",)


def test_schedule_name_with_spaces_reads_back_intact():
    schedules = {"my sched": _sched(cron="0 3 * * 1", name="my sched")}
    installed, diff = _install_list_diff(schedules)
    assert installed["my sched"].schedule == "0 3 * * 1"
    assert diff.unchanged == ("my sched",)


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


def test_run_schedule_passes_argv_verbatim_never_injects_flags():
    # a gc schedule has no --headless (it's exempt); run_schedule must not add one
    gc = Schedule(name="cleanup", cron="0 4 * * 0", run=("gc", "--keep-days", "30"))
    runner = FakeRunner({("cairn", "gc"): RunResult(0, "")})
    run_schedule({"cleanup": gc}, "cleanup", workspace_dir=WS, runner=runner)
    assert runner.calls[-1]["argv"] == ["cairn", "gc", "--keep-days", "30"]
    assert "--headless" not in runner.calls[-1]["argv"]


def test_run_schedule_reemits_captured_streams_to_injected_buffers():
    # A halted firing (NEEDS_HUMAN=6) captures output in the Runner — re-emit it so cron mails
    # it instead of silently rotting (SCHEDULING.md §4). Resume hint on stderr must survive too.
    import io

    runner = FakeRunner(
        {("cairn", "run"): RunResult(6, "run halted at gate\n", "resume: cairn resume acme-x\n")}
    )
    out, err = io.StringIO(), io.StringIO()
    code = run_schedule(
        {"weekly": _sched()}, "weekly", workspace_dir=WS, runner=runner, out=out, err=err
    )
    assert code == 6
    assert out.getvalue() == "run halted at gate\n"
    assert err.getvalue() == "resume: cairn resume acme-x\n"


def test_run_schedule_stays_silent_when_no_buffers_given():
    # backward compat: without out/err, the captured streams are simply not re-emitted
    runner = FakeRunner({("cairn", "run"): RunResult(6, "noisy stdout", "noisy stderr")})
    code = run_schedule({"weekly": _sched()}, "weekly", workspace_dir=WS, runner=runner)
    assert code == 6  # return semantics unchanged, no exception, nothing written anywhere
