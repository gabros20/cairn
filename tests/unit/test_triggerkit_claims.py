"""Inbox scan + claim/consume engine — at-most-once semantics (TRIGGERS-PLAN.md §2)."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.triggerkit import (
    Trigger,
    claim,
    consume,
    scan_candidates,
    stuck_claims,
    watch_dir,
)


def _watch(tmp_path: Path) -> Path:
    d = tmp_path / "inbox" / "replies"
    d.mkdir(parents=True)
    return d


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


def test_claim_and_consume_unicode_filename(tmp_path):
    watch = _watch(tmp_path)
    candidate = watch / "événement (1).json"
    candidate.write_text("{}", encoding="utf-8")

    claimed = claim(watch, candidate)
    assert claimed == watch / ".claim" / "événement (1).json"

    dest = consume(watch, claimed, ok=True, on_done="done")
    assert dest == watch / ".done" / "événement (1).json"
    assert dest.is_file()


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
# consume
# --------------------------------------------------------------------------- #


def test_consume_ok_done_moves_to_done_dir(tmp_path):
    watch = _watch(tmp_path)
    candidate = watch / "event.json"
    candidate.write_text("{}", encoding="utf-8")
    claimed = claim(watch, candidate)

    dest = consume(watch, claimed, ok=True, on_done="done")

    assert dest == watch / ".done" / "event.json"
    assert dest.is_file()
    assert not claimed.exists()


def test_consume_ok_delete_removes_file(tmp_path):
    watch = _watch(tmp_path)
    candidate = watch / "event.json"
    candidate.write_text("{}", encoding="utf-8")
    claimed = claim(watch, candidate)

    dest = consume(watch, claimed, ok=True, on_done="delete")

    assert dest is None
    assert not claimed.exists()
    assert not (watch / ".done").exists() or not any((watch / ".done").iterdir())


def test_consume_not_ok_moves_to_failed_dir_regardless_of_on_done(tmp_path):
    watch = _watch(tmp_path)
    candidate = watch / "event.json"
    candidate.write_text("{}", encoding="utf-8")
    claimed = claim(watch, candidate)

    dest = consume(watch, claimed, ok=False, on_done="delete")

    assert dest == watch / ".failed" / "event.json"
    assert dest.is_file()
    assert not claimed.exists()


def test_consume_collision_appends_v2_suffix(tmp_path):
    watch = _watch(tmp_path)
    done_dir = watch / ".done"
    done_dir.mkdir()
    (done_dir / "event.json").write_text("existing", encoding="utf-8")

    candidate = watch / "event.json"
    candidate.write_text("{}", encoding="utf-8")
    claimed = claim(watch, candidate)

    dest = consume(watch, claimed, ok=True, on_done="done")

    assert dest == done_dir / "event-v2.json"
    assert (done_dir / "event.json").read_text(encoding="utf-8") == "existing"
    assert dest.read_text(encoding="utf-8") == "{}"


def test_consume_collision_increments_past_v2(tmp_path):
    watch = _watch(tmp_path)
    done_dir = watch / ".done"
    done_dir.mkdir()
    (done_dir / "event.json").write_text("v1", encoding="utf-8")
    (done_dir / "event-v2.json").write_text("v2", encoding="utf-8")

    candidate = watch / "event.json"
    candidate.write_text("v3", encoding="utf-8")
    claimed = claim(watch, candidate)

    dest = consume(watch, claimed, ok=True, on_done="done")

    assert dest == done_dir / "event-v3.json"
    assert dest.read_text(encoding="utf-8") == "v3"


def test_consume_symlinked_claim_moves_the_symlink_not_its_target(tmp_path):
    watch = _watch(tmp_path)
    target = tmp_path / "outside_target.json"
    target.write_text("TARGET-DATA", encoding="utf-8")
    link = watch / "event.json"
    link.symlink_to(target)
    claimed = claim(watch, link)

    dest = consume(watch, claimed, ok=True, on_done="done")

    assert dest == watch / ".done" / "event.json"
    assert dest.is_symlink()
    assert os.path.realpath(dest) == os.path.realpath(target)
    assert target.read_text(encoding="utf-8") == "TARGET-DATA"
    assert not claimed.exists()


def test_consume_never_overwrites_existing_consumed_file(tmp_path):
    watch = _watch(tmp_path)
    done_dir = watch / ".done"
    done_dir.mkdir()
    (done_dir / "event.json").write_text("original", encoding="utf-8")

    candidate = watch / "event.json"
    candidate.write_text("new", encoding="utf-8")
    claimed = claim(watch, candidate)
    consume(watch, claimed, ok=True, on_done="done")

    assert (done_dir / "event.json").read_text(encoding="utf-8") == "original"


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

    # No consume() call — simulates a crash mid-run.
    assert stuck_claims(watch) == [claimed]


def test_stuck_claims_empty_after_consume(tmp_path):
    watch = _watch(tmp_path)
    candidate = watch / "event.json"
    candidate.write_text("{}", encoding="utf-8")
    claimed = claim(watch, candidate)
    consume(watch, claimed, ok=True, on_done="done")

    assert stuck_claims(watch) == []
