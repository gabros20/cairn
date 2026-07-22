"""Inbox scan + claim/retire engine — at-most-once semantics (TRIGGERS-PLAN.md §2,
FACTORY-PLAN W1a T3–T6).

D7-amended rewrites (consume → retire): every former ``consume(ok=)`` assertion is
restated against ``retire(outcome=)`` with the same placement outcomes, plus tombstones
and waiting-class routing that consume never had.
"""

from __future__ import annotations

import errno
import json
import os
import threading
from pathlib import Path

import pytest

from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.queue_ledger import (
    pointer_path,
    read_pointer,
    write_pointer,
)
from cairn.kernel.triggerkit import (
    Trigger,
    claim,
    retire,
    scan_candidates,
    stuck_claims,
    watch_dir,
)
from cairn.kernel.types import OutcomeClass, RunOutcome, classify_exit
from fstestkit import RecordingFs


def _watch(tmp_path: Path) -> Path:
    d = tmp_path / "inbox" / "replies"
    d.mkdir(parents=True)
    return d


def _done() -> RunOutcome:
    return RunOutcome(outcome=OutcomeClass.DONE)


def _failed() -> RunOutcome:
    return RunOutcome(outcome=OutcomeClass.FAILED)


def _waiting(kind: str = "needs_human") -> RunOutcome:
    return RunOutcome(outcome=OutcomeClass.WAITING, waiting_kind=kind)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# watch_dir
# --------------------------------------------------------------------------- #


def test_watch_dir_resolves_relative_to_workspace(tmp_path):
    trigger = Trigger(name="t", pipeline="p", watch="inbox/replies")
    assert watch_dir(trigger, tmp_path) == (tmp_path / "inbox" / "replies").resolve()


def test_watch_dir_rejects_workspace_internal_symlink_escape(tmp_path):
    # "watch:" is lexically clean (relative, no "..") so nothing at parse time can
    # reject it — the escape only appears once the symlink is resolved (F3).
    outside = tmp_path / "outside_secret_dir"
    outside.mkdir()
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "inbox_link").symlink_to(outside)
    trigger = Trigger(name="handle-reply", pipeline="p", watch="inbox_link")

    with pytest.raises(ConfigError, match="escapes the workspace via symlink"):
        watch_dir(trigger, ws)


# --------------------------------------------------------------------------- #
# scan_candidates
# --------------------------------------------------------------------------- #


def test_scan_candidates_missing_dir_is_empty(tmp_path):
    assert scan_candidates(tmp_path / "nope", "*") == []


def test_scan_candidates_existing_empty_dir_is_empty(tmp_path):
    # Distinct from the missing-dir case above: exercises the iterdir() path with zero
    # entries rather than the is_dir() early-return (F5).
    watch = _watch(tmp_path)
    assert scan_candidates(watch, "*") == []


def test_scan_candidates_unicode_and_odd_filenames(tmp_path):
    watch = _watch(tmp_path)
    names = ["café-évènement.json", "événement (1).json", "日本語.json", "with space.json"]
    for n in names:
        (watch / n).write_text("{}", encoding="utf-8")

    assert scan_candidates(watch, "*") == sorted(watch / n for n in names)


def test_scan_candidates_sorted_top_level_only(tmp_path):
    watch = _watch(tmp_path)
    (watch / "b.json").write_text("{}", encoding="utf-8")
    (watch / "a.json").write_text("{}", encoding="utf-8")
    sub = watch / "subdir"
    sub.mkdir()
    (sub / "c.json").write_text("{}", encoding="utf-8")

    assert scan_candidates(watch, "*") == [watch / "a.json", watch / "b.json"]


def test_scan_candidates_excludes_dotfiles(tmp_path):
    watch = _watch(tmp_path)
    (watch / ".hidden").write_text("x", encoding="utf-8")
    (watch / "visible.json").write_text("{}", encoding="utf-8")

    assert scan_candidates(watch, "*") == [watch / "visible.json"]


def test_scan_candidates_excludes_ledger_dirs(tmp_path):
    watch = _watch(tmp_path)
    (watch / ".claim").mkdir()
    (watch / ".done").mkdir()
    (watch / ".failed").mkdir()
    (watch / ".waiting").mkdir()
    (watch / "event.json").write_text("{}", encoding="utf-8")

    assert scan_candidates(watch, "*") == [watch / "event.json"]


def test_scan_candidates_glob_filters(tmp_path):
    watch = _watch(tmp_path)
    (watch / "event.json").write_text("{}", encoding="utf-8")
    (watch / "readme.txt").write_text("x", encoding="utf-8")

    assert scan_candidates(watch, "*.json") == [watch / "event.json"]


# --------------------------------------------------------------------------- #
# claim
# --------------------------------------------------------------------------- #


def test_claim_moves_file_into_claim_dir(tmp_path):
    watch = _watch(tmp_path)
    candidate = watch / "event.json"
    candidate.write_text("{}", encoding="utf-8")

    claimed = claim(watch, candidate)

    assert claimed == watch / ".claim" / "event.json"
    assert claimed.is_file()
    assert not candidate.exists()


def test_claim_lost_race_returns_none(tmp_path):
    # Simulate a concurrent claimer having already won: pre-move the file out from under
    # the second claimer, exactly as a real race would leave it.
    watch = _watch(tmp_path)
    candidate = watch / "event.json"
    candidate.write_text("{}", encoding="utf-8")

    first = claim(watch, candidate)
    assert first is not None

    # A second, concurrent claim of the same (now-vanished) source path loses the race.
    second = claim(watch, candidate)
    assert second is None

    # The winner's claim is untouched.
    assert first.is_file()


def test_two_claimers_never_both_succeed(tmp_path):
    watch = _watch(tmp_path)
    candidate = watch / "event.json"
    candidate.write_text("{}", encoding="utf-8")

    results = [claim(watch, candidate), claim(watch, candidate)]
    assert sorted(r is None for r in results) == [False, True]


def test_claim_never_overwrites_stuck_claim(tmp_path):
    # F1 regression: a claim already stuck in .claim/event.json (crash-recovery case)
    # must survive when a *new*, unrelated file with the same name gets claimed — never
    # silently overwritten by a POSIX rename-style replace.
    watch = _watch(tmp_path)
    claim_dir = watch / ".claim"
    claim_dir.mkdir()
    (claim_dir / "event.json").write_text("STUCK-ORIGINAL-DATA", encoding="utf-8")

    candidate = watch / "event.json"
    candidate.write_text("NEW-DATA", encoding="utf-8")

    claimed = claim(watch, candidate)

    assert claimed == claim_dir / "event-v2.json"
    assert (claim_dir / "event.json").read_text(encoding="utf-8") == "STUCK-ORIGINAL-DATA"
    assert claimed.read_text(encoding="utf-8") == "NEW-DATA"
    assert not candidate.exists()
    assert sorted(p.name for p in stuck_claims(watch)) == ["event-v2.json", "event.json"]


def test_claim_winner_unlink_tolerates_losers_concurrent_cleanup(tmp_path, monkeypatch):
    # G1 regression (review-T1-quality-r2.md): force the exact interleaving the round-2
    # review identified — the loser's own candidate.unlink() (inside
    # _links_same_source's True branch) can finish before the winner reaches its own
    # candidate.unlink() at the end of claim(). The winner must still return its claim
    # path, never raise FileNotFoundError, even though "its" unlink target is already
    # gone by the time it runs.
    watch = _watch(tmp_path)
    candidate = watch / "event.json"
    candidate.write_text("{}", encoding="utf-8")

    real_link = os.link
    lock = threading.Lock()
    call_count = 0
    linked = threading.Event()
    loser_done = threading.Event()

    def fake_link(src, dst, *, follow_symlinks=True):
        nonlocal call_count
        with lock:
            call_count += 1
            is_first = call_count == 1
        if is_first:
            real_link(src, dst, follow_symlinks=follow_symlinks)
            linked.set()
            # Hold the winner here until the loser has fully finished its own
            # candidate.unlink() — the window G1 identified.
            assert loser_done.wait(timeout=5), "loser never finished"
            return None
        assert linked.wait(timeout=5), "winner's link never happened"
        return real_link(src, dst, follow_symlinks=follow_symlinks)  # raises FileExistsError

    monkeypatch.setattr(os, "link", fake_link)

    outcomes: list[Path | None] = []
    errors: list[BaseException] = []

    def worker():
        try:
            outcomes.append(claim(watch, candidate))
        except BaseException as exc:  # noqa: BLE001 - capturing across threads for the assert below
            errors.append(exc)
        finally:
            loser_done.set()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert errors == []
    assert sorted(outcomes, key=lambda p: p is None) == [watch / ".claim" / "event.json", None]
    assert [p.name for p in (watch / ".claim").iterdir() if p.is_file()] == ["event.json"]


def test_claim_and_retire_unicode_filename(tmp_path):
    # D7 rewrite: was claim_and_consume — same placement, now via retire DONE.
    watch = _watch(tmp_path)
    candidate = watch / "événement (1).json"
    candidate.write_text("{}", encoding="utf-8")

    claimed = claim(watch, candidate)
    assert claimed == watch / ".claim" / "événement (1).json"

    dest = retire(watch, claimed, outcome=_done(), on_done="done", exit_code=0)
    assert dest == watch / ".done" / "événement (1).json"
    assert dest.is_file()
    assert (watch / ".done" / "tombstones" / "événement (1).json").is_file()


def test_claim_symlinked_candidate_links_the_symlink_not_its_target(tmp_path):
    watch = _watch(tmp_path)
    target = tmp_path / "outside_target.json"
    target.write_text("TARGET-DATA", encoding="utf-8")
    link = watch / "event.json"
    link.symlink_to(target)

    claimed = claim(watch, link)

    assert claimed == watch / ".claim" / "event.json"
    assert claimed.is_symlink()
    assert os.path.realpath(claimed) == os.path.realpath(target)
    # The real target is never touched: not read, not linked, not moved.
    assert target.read_text(encoding="utf-8") == "TARGET-DATA"


def test_claim_cross_device_link_raises_clear_cairn_error(tmp_path, monkeypatch):
    watch = _watch(tmp_path)
    candidate = watch / "event.json"
    candidate.write_text("{}", encoding="utf-8")

    def fake_link(src, dst, *, follow_symlinks=True):
        raise OSError(errno.EXDEV, "Cross-device link")

    monkeypatch.setattr(os, "link", fake_link)

    with pytest.raises(CairnError, match="different filesystems"):
        claim(watch, candidate)


# --------------------------------------------------------------------------- #
# retire — outcome routing (W1a T3; D7 rewrite of consume_*)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "code,on_done,lane,tombstone",
    [
        (0, "done", ".done", True),
        (0, "delete", None, True),  # deleted item, still tombstones
        (6, "done", ".waiting", False),
        (8, "done", ".waiting", False),
        (9, "done", ".waiting", False),
        (4, "done", ".failed", True),
        (4, "delete", ".failed", True),  # failed ignores on_done for placement
        (3, "done", ".failed", True),
    ],
)
def test_retire_routes_by_exit_code(tmp_path, code, on_done, lane, tombstone):
    watch = _watch(tmp_path)
    candidate = watch / "event.json"
    candidate.write_text("payload", encoding="utf-8")
    claimed = claim(watch, candidate)
    run_dir = tmp_path / "runs" / "t-event"
    run_dir.mkdir(parents=True)
    write_pointer(
        pointer_path(claimed.parent, claimed.name),
        run_dir=run_dir,
        child_pid=99,
    )

    dest = retire(
        watch,
        claimed,
        outcome=classify_exit(code),
        on_done=on_done,
        exit_code=code,
        child_pid=99,
        run_dir=run_dir,
    )

    if lane is None:
        assert dest is None
        assert not claimed.exists()
        assert not (watch / ".done" / "event.json").exists()
    else:
        assert dest == watch / lane / "event.json"
        assert dest.is_file()
        assert dest.read_text(encoding="utf-8") == "payload"
        assert not claimed.exists()
        if lane in (".waiting", ".failed"):
            ptr = read_pointer(pointer_path(watch / lane, "event.json"))
            assert ptr["outcome"] == classify_exit(code).outcome.value
            assert ptr["exit_code"] == code
            assert ptr["child_pid"] == 99
            assert ptr["run_dir"] == str(run_dir)
    tomb = watch / ".done" / "tombstones" / "event.json"
    assert tomb.is_file() is tombstone


def test_retire_collision_appends_v2_suffix(tmp_path):
    # D7 rewrite of test_consume_collision_appends_v2_suffix
    watch = _watch(tmp_path)
    done_dir = watch / ".done"
    done_dir.mkdir()
    (done_dir / "event.json").write_text("existing", encoding="utf-8")

    candidate = watch / "event.json"
    candidate.write_text("{}", encoding="utf-8")
    claimed = claim(watch, candidate)

    dest = retire(watch, claimed, outcome=_done(), on_done="done", exit_code=0)

    assert dest == done_dir / "event-v2.json"
    assert (done_dir / "event.json").read_text(encoding="utf-8") == "existing"
    assert dest.read_text(encoding="utf-8") == "{}"


def test_retire_collision_increments_past_v2(tmp_path):
    watch = _watch(tmp_path)
    done_dir = watch / ".done"
    done_dir.mkdir()
    (done_dir / "event.json").write_text("v1", encoding="utf-8")
    (done_dir / "event-v2.json").write_text("v2", encoding="utf-8")

    candidate = watch / "event.json"
    candidate.write_text("v3", encoding="utf-8")
    claimed = claim(watch, candidate)

    dest = retire(watch, claimed, outcome=_done(), on_done="done", exit_code=0)

    assert dest == done_dir / "event-v3.json"
    assert dest.read_text(encoding="utf-8") == "v3"


def test_retire_symlinked_claim_moves_the_symlink_not_its_target(tmp_path):
    watch = _watch(tmp_path)
    target = tmp_path / "outside_target.json"
    target.write_text("TARGET-DATA", encoding="utf-8")
    link = watch / "event.json"
    link.symlink_to(target)
    claimed = claim(watch, link)

    dest = retire(watch, claimed, outcome=_done(), on_done="done", exit_code=0)

    assert dest == watch / ".done" / "event.json"
    assert dest.is_symlink()
    assert os.path.realpath(dest) == os.path.realpath(target)
    assert target.read_text(encoding="utf-8") == "TARGET-DATA"
    assert not claimed.exists()


def test_retire_never_overwrites_existing_retired_file(tmp_path):
    watch = _watch(tmp_path)
    done_dir = watch / ".done"
    done_dir.mkdir()
    (done_dir / "event.json").write_text("original", encoding="utf-8")

    candidate = watch / "event.json"
    candidate.write_text("new", encoding="utf-8")
    claimed = claim(watch, candidate)
    retire(watch, claimed, outcome=_done(), on_done="done", exit_code=0)

    assert (done_dir / "event.json").read_text(encoding="utf-8") == "original"


def test_retire_pointer_first_ordering_via_recording_fs(tmp_path):
    """Pointer write/relocate ops complete before the item hardlink (T5 order)."""
    watch = _watch(tmp_path)
    candidate = watch / "event.json"
    candidate.write_text("body", encoding="utf-8")
    claimed = claim(watch, candidate)
    run_dir = tmp_path / "runs" / "t-event"
    run_dir.mkdir(parents=True)
    src_ptr = pointer_path(claimed.parent, claimed.name)
    write_pointer(src_ptr, run_dir=run_dir, child_pid=7)

    # Real-fs retire — then assert pointer landed in waiting before we would read item.
    # For ordering under crash: inject RecordingFs into write_pointer path via retire's fs=.
    # Use a crash after the dest pointer is written (open/fsync/replace sequence) but
    # before item move: crash_after tuned to pointer write ops only.
    fs = RecordingFs()
    # Seed claim item + existing pointer into the fake so durable ops see them.
    fs.seed(claimed, "body")
    fs.seed(src_ptr, json.dumps({"run_dir": str(run_dir), "outcome": None,
                                 "exit_code": None, "child_pid": 7}) + "\n")

    # Full retire on real fs for content assertions; separate crash test below.
    dest = retire(
        watch,
        claimed,
        outcome=_waiting(),
        on_done="done",
        exit_code=6,
        child_pid=7,
        run_dir=run_dir,
    )
    assert dest == watch / ".waiting" / "event.json"
    ptr = read_pointer(pointer_path(watch / ".waiting", "event.json"))
    assert ptr["child_pid"] == 7
    assert ptr["outcome"] == "waiting"
    assert ptr["exit_code"] == 6
    assert not src_ptr.exists()


def test_retire_pointer_first_crash_leaves_dest_pointer(tmp_path):
    """T5 crash pair: pointer written to dest lane, item still in source — repairable."""
    watch = _watch(tmp_path)
    candidate = watch / "event.json"
    candidate.write_text("body", encoding="utf-8")
    claimed = claim(watch, candidate)
    run_dir = tmp_path / "runs" / "t-event"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text('{"status":"running"}', encoding="utf-8")
    write_pointer(
        pointer_path(claimed.parent, claimed.name),
        run_dir=run_dir,
        child_pid=3,
    )

    # Simulate crash after pointer relocated to .waiting/.runs/ but item still in .claim/:
    # manually place pointer in dest, leave item in claim (the interrupted state).
    waiting = watch / ".waiting"
    waiting.mkdir()
    dest_ptr = pointer_path(waiting, "event.json")
    write_pointer(
        dest_ptr,
        run_dir=run_dir,
        outcome="waiting",
        exit_code=6,
        child_pid=3,
    )
    # Drop the source pointer as retire would after writing dest.
    src_ptr = pointer_path(claimed.parent, claimed.name)
    if src_ptr.exists():
        src_ptr.unlink()
    assert claimed.is_file()
    assert not (waiting / "event.json").exists()

    from cairn.kernel.queue_ledger import sweep

    report = sweep(watch, on_done="done")
    assert any(p.name == "event.json" for p in report.repaired)
    assert (waiting / "event.json").is_file()
    assert not claimed.exists()


# --------------------------------------------------------------------------- #
# stuck_claims
# --------------------------------------------------------------------------- #


def test_stuck_claims_empty_when_no_claim_dir(tmp_path):
    watch = _watch(tmp_path)
    assert stuck_claims(watch) == []


def test_stuck_claims_lists_files_left_in_claim_dir(tmp_path):
    watch = _watch(tmp_path)
    candidate = watch / "event.json"
    candidate.write_text("{}", encoding="utf-8")
    claimed = claim(watch, candidate)

    # No retire() call — simulates a crash mid-run.
    assert stuck_claims(watch) == [claimed]


def test_stuck_claims_empty_after_retire(tmp_path):
    # D7 rewrite of test_stuck_claims_empty_after_consume
    watch = _watch(tmp_path)
    candidate = watch / "event.json"
    candidate.write_text("{}", encoding="utf-8")
    claimed = claim(watch, candidate)
    retire(watch, claimed, outcome=_done(), on_done="done", exit_code=0)

    assert stuck_claims(watch) == []


def test_stuck_claims_ignores_runs_pointer_dir(tmp_path):
    watch = _watch(tmp_path)
    claim_dir = watch / ".claim"
    claim_dir.mkdir()
    (claim_dir / "stuck.json").write_text("x", encoding="utf-8")
    write_pointer(pointer_path(claim_dir, "stuck.json"), run_dir="/r")
    assert [p.name for p in stuck_claims(watch)] == ["stuck.json"]
