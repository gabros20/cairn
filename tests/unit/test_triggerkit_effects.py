"""sync_triggers / remove_trigger / list_installed_triggers / run_trigger — the
effectful verbs. Every side effect goes through a recording fake Runner + tmp target
dirs; no test ever touches the real host watcher, ~/Library, or spawns a real child
process (mirrors test_schedkit_effects.py's standard).
"""

from __future__ import annotations

import plistlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.proc import RunResult
from cairn.kernel.triggerkit import (
    list_installed_triggers,
    remove_trigger,
    run_trigger,
    sync_triggers,
    trigger_launchd_label,
    trigger_systemd_unit_names,
)

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

TRIGGERS_ONE = """\
handle-reply:
  pipeline: handle-reply
  watch: inbox/replies
"""

TRIGGERS_ONE_X = """\
X:
  pipeline: x-pipe
  watch: inbox/x
"""

_SCHEDULE_OWNED_SERVICE = (
    "[Unit]\nDescription=cairn schedule: trigger-X\n\n[Service]\nType=oneshot\n"
    "WorkingDirectory=/ws/acme\nExecStart=cairn schedule run trigger-X\n"
).encode("utf-8")


class FakeRunner:
    """Records every argv/input/cwd; returns canned results keyed by the first two argv tokens."""

    def __init__(self, canned=None):
        self.calls: list[dict] = []
        self._canned = canned or {}

    def run(self, argv, *, input=None, cwd=None) -> RunResult:
        self.calls.append({"argv": list(argv), "input": input, "cwd": cwd})
        key = tuple(argv[:2])
        return self._canned.get(key, RunResult(returncode=0, stdout="", stderr=""))


def _workspace(tmp_path: Path, triggers_yaml: str, *, pipelines=("handle-reply",)) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "pipelines").mkdir()
    for p in pipelines:
        (ws / "pipelines" / f"{p}.yaml").write_text("nodes: {}\n", encoding="utf-8")
    (ws / "triggers.yaml").write_text(triggers_yaml, encoding="utf-8")
    return ws


# --- sync_triggers: launchd ---------------------------------------------------


def test_sync_launchd_writes_plist_and_loads_via_runner(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    launchd_dir = tmp_path / "launchd"
    runner = FakeRunner()
    touched = sync_triggers(ws, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)
    plist = launchd_dir / f"{trigger_launchd_label('handle-reply')}.plist"
    assert plist.is_file()
    doc = plistlib.loads(plist.read_bytes())
    assert doc["ProgramArguments"][:3] == ["cairn", "trigger", "run"]
    assert touched == [plist.name]
    assert any(c["argv"][:2] == ["launchctl", "unload"] for c in runner.calls)
    assert any(c["argv"][:2] == ["launchctl", "load"] for c in runner.calls)


# --- sync_triggers: systemd ---------------------------------------------------


def test_sync_systemd_writes_units_and_enables_path_unit(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    systemd_dir = tmp_path / "systemd"
    runner = FakeRunner()
    touched = sync_triggers(ws, backend="systemd", runner=runner, cairn_bin="cairn", systemd_dir=systemd_dir)
    path_name, service_name = trigger_systemd_unit_names("handle-reply")
    assert (systemd_dir / path_name).is_file()
    assert (systemd_dir / service_name).is_file()
    assert set(touched) == {path_name, service_name}
    assert any(c["argv"] == ["systemctl", "--user", "daemon-reload"] for c in runner.calls)
    assert any(c["argv"] == ["systemctl", "--user", "enable", "--now", path_name] for c in runner.calls)


# --- sync_triggers: backend errors --------------------------------------------


def test_sync_cron_backend_refuses_with_documented_fallback(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    with pytest.raises(ConfigError, match="schedules.yaml"):
        sync_triggers(ws, backend="cron", runner=FakeRunner(), cairn_bin="cairn")
    with pytest.raises(ConfigError, match=r"trigger.*run"):
        sync_triggers(ws, backend="cron", runner=FakeRunner(), cairn_bin="cairn")


def test_sync_unknown_backend_is_config_error(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    with pytest.raises(ConfigError, match="unknown backend"):
        sync_triggers(ws, backend="nope", runner=FakeRunner(), cairn_bin="cairn")


# --- sync_triggers: ownership refusal (addendum 2 / spec finding F1) ---------


def test_sync_refuses_to_overwrite_a_schedule_owned_unit_file(tmp_path):
    # A schedule named "trigger-X" and a trigger named "X" both render
    # cairn-trigger-X.service (T2's namespace cannot rule this out) — sync must refuse
    # loud rather than overwrite the schedule's live unit file.
    ws = _workspace(tmp_path, TRIGGERS_ONE_X, pipelines=("x-pipe",))
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    service_path = systemd_dir / "cairn-trigger-X.service"
    service_path.write_bytes(_SCHEDULE_OWNED_SERVICE)
    runner = FakeRunner()
    with pytest.raises(ConfigError, match="schedule"):
        sync_triggers(ws, backend="systemd", runner=runner, cairn_bin="cairn", systemd_dir=systemd_dir)
    assert service_path.read_bytes() == _SCHEDULE_OWNED_SERVICE
    assert not (systemd_dir / "cairn-trigger-X.path").exists()  # nothing else got written either
    assert runner.calls == []  # refused before any runner call


def test_sync_refuses_to_overwrite_an_unmanaged_launchd_plist(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    label = trigger_launchd_label("handle-reply")
    foreign = launchd_dir / f"{label}.plist"
    foreign.write_text("not a plist at all", encoding="utf-8")
    with pytest.raises(ConfigError, match="unmanaged"):
        sync_triggers(ws, backend="launchd", runner=FakeRunner(), cairn_bin="cairn", launchd_dir=launchd_dir)
    assert foreign.read_text(encoding="utf-8") == "not a plist at all"


# --- remove_trigger ------------------------------------------------------------


def test_remove_trigger_launchd_removes_plist_and_unloads(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    launchd_dir = tmp_path / "launchd"
    runner = FakeRunner()
    sync_triggers(ws, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)
    plist = launchd_dir / f"{trigger_launchd_label('handle-reply')}.plist"
    assert plist.is_file()
    removed = remove_trigger("handle-reply", ws, backend="launchd", runner=runner, launchd_dir=launchd_dir)
    assert removed is True
    assert not plist.exists()
    assert any(c["argv"][:2] == ["launchctl", "unload"] for c in runner.calls)


def test_remove_trigger_returns_false_when_nothing_installed(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    launchd_dir = tmp_path / "launchd"
    launchd_dir.mkdir()
    removed = remove_trigger("handle-reply", ws, backend="launchd", runner=FakeRunner(), launchd_dir=launchd_dir)
    assert removed is False


def test_remove_trigger_systemd_removes_both_units_and_reloads(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    systemd_dir = tmp_path / "systemd"
    runner = FakeRunner()
    sync_triggers(ws, backend="systemd", runner=runner, cairn_bin="cairn", systemd_dir=systemd_dir)
    path_name, service_name = trigger_systemd_unit_names("handle-reply")
    assert (systemd_dir / path_name).is_file()
    assert (systemd_dir / service_name).is_file()
    removed = remove_trigger("handle-reply", ws, backend="systemd", runner=runner, systemd_dir=systemd_dir)
    assert removed is True
    assert not (systemd_dir / path_name).exists()
    assert not (systemd_dir / service_name).exists()
    assert any(c["argv"] == ["systemctl", "--user", "daemon-reload"] for c in runner.calls)


def test_remove_trigger_refuses_to_delete_a_schedule_owned_file(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE_X, pipelines=("x-pipe",))
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    service_path = systemd_dir / "cairn-trigger-X.service"
    service_path.write_bytes(_SCHEDULE_OWNED_SERVICE)
    with pytest.raises(ConfigError, match="schedule"):
        remove_trigger("X", ws, backend="systemd", runner=FakeRunner(), systemd_dir=systemd_dir)
    assert service_path.read_bytes() == _SCHEDULE_OWNED_SERVICE


def test_remove_trigger_cron_backend_refuses(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    with pytest.raises(ConfigError, match="schedules.yaml"):
        remove_trigger("handle-reply", ws, backend="cron", runner=FakeRunner())


# --- list_installed_triggers ---------------------------------------------------


def test_list_installed_triggers_reports_declared_installed_and_stuck(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    launchd_dir = tmp_path / "launchd"
    runner = FakeRunner()
    sync_triggers(ws, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)

    watch_abs = ws / "inbox" / "replies"
    claim_dir = watch_abs / ".claim"
    claim_dir.mkdir(parents=True)
    (claim_dir / "orphan.json").write_text("stuck", encoding="utf-8")

    statuses = list_installed_triggers(ws, backend="launchd", runner=runner, launchd_dir=launchd_dir)
    assert len(statuses) == 1
    status = statuses[0]
    assert status.name == "handle-reply"
    assert status.declared is True
    assert status.installed is True
    assert [p.name for p in status.stuck] == ["orphan.json"]


def test_list_installed_triggers_declared_not_installed_and_installed_not_declared(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE, pipelines=("handle-reply", "other"))
    launchd_dir = tmp_path / "launchd"
    runner = FakeRunner()
    sync_triggers(ws, backend="launchd", runner=runner, cairn_bin="cairn", launchd_dir=launchd_dir)
    # declare a second trigger that has not been synced yet
    (ws / "triggers.yaml").write_text(
        TRIGGERS_ONE + "other:\n  pipeline: other\n  watch: inbox/other\n", encoding="utf-8"
    )
    statuses = {
        s.name: s for s in list_installed_triggers(ws, backend="launchd", runner=runner, launchd_dir=launchd_dir)
    }
    assert statuses["handle-reply"].declared and statuses["handle-reply"].installed
    assert statuses["other"].declared and not statuses["other"].installed


def test_list_installed_triggers_cron_backend_refuses(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    with pytest.raises(ConfigError, match="schedules.yaml"):
        list_installed_triggers(ws, backend="cron", runner=FakeRunner())


# --- run_trigger -----------------------------------------------------------


def test_run_trigger_drains_one_candidate_and_consumes_to_done(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    (watch_abs / "one.json").write_text("event payload", encoding="utf-8")

    runner = FakeRunner({("cairn", "run"): RunResult(0, "", "")})
    code = run_trigger("handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW)

    assert code == 0
    call = runner.calls[-1]
    claimed_path = watch_abs / ".done" / "one.json"
    assert claimed_path.is_file()
    assert call["argv"] == [
        "cairn",
        "run",
        "handle-reply",
        "--headless",
        "--param",
        f"event={watch_abs / '.claim' / 'one.json'}",
    ]
    assert call["cwd"] == ws


def test_run_trigger_empty_scan_returns_zero_and_calls_nothing(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    runner = FakeRunner()
    assert run_trigger("handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW) == 0
    assert runner.calls == []


def test_run_trigger_unknown_name_lists_declared_triggers(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    with pytest.raises(ConfigError, match="handle-reply"):
        run_trigger("ghost", ws, runner=FakeRunner(), cairn_bin="cairn", now=NOW)


def test_run_trigger_drain_continues_past_a_failed_child(tmp_path):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    # "fail-me" sorts before "ok" — proves the SECOND candidate still runs after the
    # first one's child fails (never stop at the first bad event).
    (watch_abs / "fail-me.json").write_text("x", encoding="utf-8")
    (watch_abs / "ok.json").write_text("y", encoding="utf-8")

    class SelectiveRunner:
        def __init__(self):
            self.calls: list[dict] = []

        def run(self, argv, *, input=None, cwd=None):
            self.calls.append({"argv": list(argv), "cwd": cwd})
            ok = "fail-me" not in argv[-1]
            return RunResult(returncode=0 if ok else 3)

    runner = SelectiveRunner()
    code = run_trigger("handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW)

    assert code != 0  # at least one child failed
    assert len(runner.calls) == 2  # BOTH candidates were run, not just the first
    assert (watch_abs / ".failed" / "fail-me.json").is_file()
    assert (watch_abs / ".done" / "ok.json").is_file()


def test_run_trigger_uses_the_v2_suffixed_claim_path_when_a_stuck_claim_collides(tmp_path):
    # addendum: claim()'s returned path may carry a -v2 suffix when a same-named stuck
    # claim already sits in .claim/ — run_trigger must operate on the RETURNED path,
    # never reconstruct it from the candidate's own basename.
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    claim_dir = watch_abs / ".claim"
    claim_dir.mkdir()
    (claim_dir / "one.json").write_text("stuck from a crashed prior firing", encoding="utf-8")
    (watch_abs / "one.json").write_text("a new event, same filename", encoding="utf-8")

    runner = FakeRunner({("cairn", "run"): RunResult(0, "", "")})
    code = run_trigger("handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW)

    assert code == 0
    call = runner.calls[-1]
    assert call["argv"][-1] == f"event={watch_abs / '.claim' / 'one-v2.json'}"
    assert (watch_abs / ".done" / "one-v2.json").is_file()
    # the original stuck claim is untouched — still sitting in .claim/, never overwritten
    assert (claim_dir / "one.json").read_text(encoding="utf-8") == "stuck from a crashed prior firing"


def test_run_trigger_claim_hazard_halts_only_that_event_not_the_whole_drain(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, TRIGGERS_ONE)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    (watch_abs / "bad.json").write_text("x", encoding="utf-8")
    (watch_abs / "good.json").write_text("y", encoding="utf-8")

    import cairn.kernel.triggerkit as tk

    real_claim = tk.claim

    def flaky_claim(watch_abs_arg, candidate):
        if candidate.name == "bad.json":
            raise CairnError("simulated cross-device hazard")
        return real_claim(watch_abs_arg, candidate)

    monkeypatch.setattr(tk, "claim", flaky_claim)
    runner = FakeRunner({("cairn", "run"): RunResult(0, "", "")})
    code = run_trigger("handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW)

    assert code != 0  # the hazard counts as a failure
    assert (watch_abs / "bad.json").is_file()  # never claimed — left exactly where it was
    assert not (watch_abs / ".failed" / "bad.json").exists()  # nothing to consume — never claimed
    assert (watch_abs / ".done" / "good.json").is_file()  # drain continued past the hazard
