"""W3 identity (T1 / T12): grammar, envelope, reservation, tombstone dedupe, .deferred.

identity:strict is opt-in; identity:off stays byte-identical to pre-T12 (D7).
Puller filename GENERATION is W4/OUT — tests hand-craft conforming names.
"""

from __future__ import annotations

import io
import json
import os
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cairn.kernel.durafs import exclusive_create
from cairn.kernel.errors import ConfigError
from cairn.kernel.proc import RunResult, RunnerBase
from cairn.kernel.queue_drain import run_trigger
from cairn.kernel.queue_ledger import (
    DEFAULT_MAX_ITEM_BYTES,
    NAME_MAX,
    RESERVATION_GRACE_S,
    ItemId,
    RetryError,
    RetryPrepared,
    RetryRefused,
    admit_strict,
    claim,
    parse_item_name,
    pointer_path,
    prepare_failed_retry,
    promote_deferred,
    read_receipt_rev,
    release_identity_on_terminal,
    release_orphan_reservations,
    release_reservation,
    reservation_path,
    reserve_identity,
    retire,
    rev_order,
    scan_candidates,
    write_pointer,
)
from cairn.kernel.runstate import create_run
from cairn.kernel.trigger_host import load_triggers
from cairn.kernel.types import OutcomeClass, RunOutcome
from fstestkit import _CannedHandle

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class FakeRunner(RunnerBase):
    def __init__(self, canned=None):
        self.calls: list[dict] = []
        self._canned = canned or {}

    def spawn(self, argv, *, input=None, cwd=None) -> _CannedHandle:
        self.calls.append({"argv": list(argv), "input": input, "cwd": cwd})
        key = tuple(argv[:2])
        result = self._canned.get(key, RunResult(returncode=0, stdout="", stderr=""))
        return _CannedHandle(result)


def _workspace(tmp_path: Path, triggers_yaml: str, *, pipelines=("handle-reply",)) -> Path:
    (tmp_path / "triggers.yaml").write_text(textwrap.dedent(triggers_yaml), encoding="utf-8")
    pdir = tmp_path / "pipelines"
    pdir.mkdir(exist_ok=True)
    for name in pipelines:
        (pdir / f"{name}.yaml").write_text("pipeline: x\nsteps: []\n", encoding="utf-8")
    return tmp_path


def _stub_mint(monkeypatch, ws: Path):
    import cairn.kernel.queue_drain as qd
    from cairn.kernel.runctl import Minted

    def fake_preallocate(workspace_dir, trigger, claimed_path, *, now):
        run_dir = qd.run_dir_for_item(ws / "runs", trigger.name, claimed_path.name)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text('{"status":"running"}', encoding="utf-8")
        return Minted(run_dir=run_dir)

    monkeypatch.setattr(qd, "preallocate_run", fake_preallocate)


def _item_body(prio: int, source: str, id_: str, rev: str, **extra) -> str:
    doc = {"source": source, "id": id_, "rev": rev, "prio": prio, **extra}
    return json.dumps(doc)


def _drop(
    watch: Path,
    prio: int,
    source: str,
    id_: str,
    rev: str,
    *,
    body: str | None = None,
    name: str | None = None,
) -> Path:
    fname = name if name is not None else f"p{prio}-{source}-{id_}-r{rev}.json"
    path = watch / fname
    path.write_text(
        body if body is not None else _item_body(prio, source, id_, rev),
        encoding="utf-8",
    )
    return path


def _done() -> RunOutcome:
    return RunOutcome(outcome=OutcomeClass.DONE)


def _strict_ws(tmp_path: Path) -> Path:
    return _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          identity: strict
        """,
    )


# --------------------------------------------------------------------------- #
# Grammar parse table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name,expected",
    [
        (
            "p1-github-42-r100.json",
            ItemId(prio=1, source="github", id="42", rev="100"),
        ),
        (
            "p0-linear-abc-def-r9.json",
            ItemId(prio=0, source="linear", id="abc-def", rev="9"),
        ),
        (
            "p9-gh-x-r1710000000.json",
            ItemId(prio=9, source="gh", id="x", rev="1710000000"),
        ),
        (
            "p5-src-id.with.dots-r1.2.json",
            ItemId(prio=5, source="src", id="id.with.dots", rev="1.2"),
        ),
    ],
)
def test_parse_item_name_valid(name, expected):
    got = parse_item_name(name)
    assert got == expected
    assert got is not None
    assert got.identity == f"{expected.source}-{expected.id}"
    assert got.filename == name
    assert got.tombstone_name == f"{expected.identity}-r{expected.rev}"


@pytest.mark.parametrize(
    "name",
    [
        "",  # empty
        "event.json",  # no grammar
        "P1-github-42-r100.json",  # uppercase
        "p1-GitHub-42-r100.json",  # uppercase source
        "p10-github-42-r100.json",  # multi-digit prio
        "p1-github-42-r100",  # no .json
        "p1-github-42-r100.txt",  # wrong suffix
        "p1--42-r100.json",  # empty source
        "p1-github--r100.json",  # empty id
        "p1-github-42-.json",  # missing rev
        "p1-github-42-r.json",  # empty rev
        "../etc/passwd.json",  # traversal-shaped
        "p1-github/42-r100.json",  # slash
        "p1-github-42-r100.json.exe",  # trailing junk
        "xp1-github-42-r100.json",  # prefix junk
    ],
)
def test_parse_item_name_invalid(name):
    assert parse_item_name(name) is None


def test_parse_item_name_overlong_rejected():
    # Build a name that exceeds NAME_MAX.
    id_part = "x" * (NAME_MAX)
    name = f"p1-gh-{id_part}-r1.json"
    assert len(name) > NAME_MAX
    assert parse_item_name(name) is None


def test_parse_item_name_derived_tombstone_fits_name_max():
    # Valid short name — derived names fit.
    item = parse_item_name("p1-github-42-r100.json")
    assert item is not None
    assert len(item.identity) <= NAME_MAX
    assert len(item.tombstone_name) <= NAME_MAX


# --------------------------------------------------------------------------- #
# Trigger keys
# --------------------------------------------------------------------------- #


def test_load_trigger_identity_defaults_off(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
        """,
    )
    t = load_triggers(ws)["handle-reply"]
    assert t.identity == "off"
    assert t.max_item_bytes is None


def test_load_trigger_identity_strict_and_max_bytes(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          identity: strict
          max_item_bytes: 4096
        """,
    )
    t = load_triggers(ws)["handle-reply"]
    assert t.identity == "strict"
    assert t.max_item_bytes == 4096


def test_load_trigger_rejects_bad_identity(tmp_path):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          identity: maybe
        """,
    )
    with pytest.raises(ConfigError, match="identity"):
        load_triggers(ws)


# --------------------------------------------------------------------------- #
# Admission envelope → .rejected
# --------------------------------------------------------------------------- #


def test_envelope_rejects_bad_grammar(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    cand = watch / "not-a-work-item.json"
    cand.write_text('{"source":"g","id":"1","rev":"1","prio":1}', encoding="utf-8")
    r = admit_strict(watch, cand)
    assert r.disposition == "reject"
    assert not cand.exists()
    assert (watch / ".rejected" / "not-a-work-item.json").is_file()
    assert "grammar" in (r.diagnostic or "")


def test_envelope_rejects_symlink(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    target = tmp_path / "target.json"
    target.write_text(_item_body(1, "github", "1", "1"), encoding="utf-8")
    cand = watch / "p1-github-1-r1.json"
    cand.symlink_to(target)
    r = admit_strict(watch, cand)
    assert r.disposition == "reject"
    assert "symlink" in (r.diagnostic or "")
    assert (watch / ".rejected" / "p1-github-1-r1.json").is_symlink() or (
        watch / ".rejected" / "p1-github-1-r1.json"
    ).exists()


def test_envelope_rejects_oversize(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    body = _item_body(1, "github", "1", "1", pad="x" * 200)
    cand = _drop(watch, 1, "github", "1", "1", body=body)
    r = admit_strict(watch, cand, max_item_bytes=50)
    assert r.disposition == "reject"
    assert "byte cap" in (r.diagnostic or "")
    assert (watch / ".rejected" / cand.name).is_file()


def test_envelope_rejects_non_utf8(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    cand = watch / "p1-github-1-r1.json"
    cand.write_bytes(b'{"source":"github","id":"1","rev":"1","prio":1,\xff}')
    r = admit_strict(watch, cand)
    assert r.disposition == "reject"
    assert "UTF-8" in (r.diagnostic or "")


def test_envelope_rejects_non_json(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    cand = watch / "p1-github-1-r1.json"
    cand.write_text("not json at all", encoding="utf-8")
    r = admit_strict(watch, cand)
    assert r.disposition == "reject"
    assert "JSON" in (r.diagnostic or "")


def test_envelope_rejects_body_disagreement(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    # Filename says rev 1; body says rev 2.
    cand = _drop(
        watch, 1, "github", "1", "1", body=_item_body(1, "github", "1", "2")
    )
    r = admit_strict(watch, cand)
    assert r.disposition == "reject"
    assert "disagreement" in (r.diagnostic or "")
    assert (watch / ".rejected" / cand.name).is_file()


def test_envelope_rejects_json_array(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    cand = watch / "p1-github-1-r1.json"
    cand.write_text("[1,2,3]", encoding="utf-8")
    r = admit_strict(watch, cand)
    assert r.disposition == "reject"
    assert "object" in (r.diagnostic or "")


# --------------------------------------------------------------------------- #
# Reservation blocks second rev while live
# --------------------------------------------------------------------------- #


def test_reservation_blocks_second_rev_while_live(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    a = _drop(watch, 1, "github", "42", "10")
    r1 = admit_strict(watch, a)
    assert r1.disposition == "admit"
    assert reservation_path(watch, "github-42").is_file()
    claimed = claim(watch, a)
    assert claimed is not None

    b = _drop(watch, 2, "github", "42", "20")  # newer rev, same identity
    r2 = admit_strict(watch, b)
    assert r2.disposition == "defer"
    assert not b.exists()
    assert (watch / ".deferred" / "github-42").is_file()
    deferred = json.loads((watch / ".deferred" / "github-42").read_text(encoding="utf-8"))
    assert deferred["rev"] == "20"


def test_exclusive_create_returns_false_when_taken(tmp_path):
    path = tmp_path / "slot"
    assert exclusive_create(path, "first") is True
    assert path.read_text(encoding="utf-8") == "first"
    assert exclusive_create(path, "second") is False
    assert path.read_text(encoding="utf-8") == "first"


# --------------------------------------------------------------------------- #
# Tombstone dedupe + re-entry
# --------------------------------------------------------------------------- #


def test_redrop_identical_rev_skipped_and_removed(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    a = _drop(watch, 1, "github", "42", "10")
    assert admit_strict(watch, a).disposition == "admit"
    claimed = claim(watch, a)
    assert claimed is not None
    retire(watch, claimed, outcome=_done(), on_done="done", exit_code=0)
    assert (watch / ".done" / "tombstones" / "github-42-r10").is_file()
    assert not reservation_path(watch, "github-42").exists()

    # Re-drop same rev → skip + remove (already delivered).
    b = _drop(watch, 1, "github", "42", "10")
    r = admit_strict(watch, b)
    assert r.disposition == "skip"
    assert not b.exists()
    assert "dedupe" in (r.diagnostic or "")


def test_new_rev_of_retired_identity_admits(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    a = _drop(watch, 1, "github", "42", "10")
    assert admit_strict(watch, a).disposition == "admit"
    claimed = claim(watch, a)
    retire(watch, claimed, outcome=_done(), on_done="done", exit_code=0)

    b = _drop(watch, 1, "github", "42", "20")  # newer rev
    r = admit_strict(watch, b)
    assert r.disposition == "admit"
    assert reservation_path(watch, "github-42").is_file()


def test_older_rev_after_newer_tombstone_skipped(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    a = _drop(watch, 1, "github", "42", "20")
    assert admit_strict(watch, a).disposition == "admit"
    claimed = claim(watch, a)
    retire(watch, claimed, outcome=_done(), on_done="done", exit_code=0)

    older = _drop(watch, 1, "github", "42", "10")
    r = admit_strict(watch, older)
    assert r.disposition == "skip"
    assert not older.exists()


# --------------------------------------------------------------------------- #
# .deferred park + promote on retire
# --------------------------------------------------------------------------- #


def test_deferred_parks_newer_rev_then_promotes_on_retire(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    a = _drop(watch, 1, "github", "42", "10")
    assert admit_strict(watch, a).disposition == "admit"
    claimed = claim(watch, a)
    assert claimed is not None

    b = _drop(watch, 1, "github", "42", "20")
    assert admit_strict(watch, b).disposition == "defer"
    assert (watch / ".deferred" / "github-42").is_file()

    retire(watch, claimed, outcome=_done(), on_done="done", exit_code=0)
    # Reservation released; deferred promoted to inbox.
    assert not reservation_path(watch, "github-42").exists()
    assert not (watch / ".deferred" / "github-42").exists()
    promoted = watch / "p1-github-42-r20.json"
    assert promoted.is_file()
    assert json.loads(promoted.read_text(encoding="utf-8"))["rev"] == "20"


def test_deferred_stale_rev_dropped_on_retire(tmp_path):
    """Park a rev, then somehow the live item finishes at a higher rev — drop park.

    Simulated by writing a deferred with rev <= retiring rev before retire.
    """
    watch = tmp_path / "inbox"
    watch.mkdir()
    a = _drop(watch, 1, "github", "42", "20")
    assert admit_strict(watch, a).disposition == "admit"
    claimed = claim(watch, a)

    deferred = watch / ".deferred"
    deferred.mkdir()
    # Stale parked rev 10 < retiring 20.
    (deferred / "github-42").write_text(
        _item_body(1, "github", "42", "10"), encoding="utf-8"
    )
    retire(watch, claimed, outcome=_done(), on_done="done", exit_code=0)
    assert not (deferred / "github-42").exists()
    assert not (watch / "p1-github-42-r10.json").exists()
    # Stale deferred was tombstoned as already-delivered.
    assert (watch / ".done" / "tombstones" / "github-42-r10").is_file()


def test_deferred_latest_rev_wins_when_parking(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    a = _drop(watch, 1, "github", "42", "10")
    assert admit_strict(watch, a).disposition == "admit"
    claim(watch, a)

    _drop(watch, 1, "github", "42", "15")
    assert admit_strict(watch, watch / "p1-github-42-r15.json").disposition == "defer"
    _drop(watch, 1, "github", "42", "12")  # older than parked 15
    r = admit_strict(watch, watch / "p1-github-42-r12.json")
    assert r.disposition == "defer"
    deferred = json.loads((watch / ".deferred" / "github-42").read_text(encoding="utf-8"))
    assert deferred["rev"] == "15"  # kept greater


# --------------------------------------------------------------------------- #
# Orphan reservation — grace period (C1)
# --------------------------------------------------------------------------- #


def _age_reservation(path: Path, *, age_s: float, now_ts: float) -> None:
    """Set reservation mtime to now_ts - age_s (deterministic; no sleep)."""
    mtime = now_ts - age_s
    os.utime(path, (mtime, mtime))


def test_orphan_fresh_reservation_not_released(tmp_path):
    """C1: mtime = now, no live item → still within grace → NOT released."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    ids = watch / ".claim" / ".ids"
    ids.mkdir(parents=True)
    res = ids / "github-fresh"
    res.write_text("", encoding="utf-8")
    now_ts = NOW.timestamp()
    _age_reservation(res, age_s=0.0, now_ts=now_ts)

    diags = release_orphan_reservations(watch, now=now_ts)
    assert diags == []
    assert res.is_file()


def test_orphan_aged_reservation_released(tmp_path):
    """C1: mtime older than grace, no live item → released."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    ids = watch / ".claim" / ".ids"
    ids.mkdir(parents=True)
    res = ids / "github-aged"
    res.write_text("", encoding="utf-8")
    now_ts = NOW.timestamp()
    _age_reservation(res, age_s=RESERVATION_GRACE_S + 1.0, now_ts=now_ts)

    diags = release_orphan_reservations(watch, now=now_ts)
    assert any("github-aged" in d for d in diags)
    assert not res.exists()


def test_orphan_live_item_never_released_even_if_aged(tmp_path):
    """C1: reserve-then-claim in progress (item in .claim/) — never released."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    a = _drop(watch, 1, "github", "1", "1")
    assert admit_strict(watch, a).disposition == "admit"
    claim(watch, a)
    res = reservation_path(watch, "github-1")
    assert res.is_file()
    now_ts = NOW.timestamp()
    _age_reservation(res, age_s=RESERVATION_GRACE_S + 100.0, now_ts=now_ts)

    diags = release_orphan_reservations(watch, now=now_ts)
    assert diags == []
    assert res.is_file()


def test_orphan_reservation_released_by_later_drain_when_aged(tmp_path, monkeypatch):
    ws = _strict_ws(tmp_path)
    _stub_mint(monkeypatch, ws)
    watch = ws / "inbox" / "replies"
    watch.mkdir(parents=True)

    # Crash after reserve, before claim: aged reservation, no live item.
    ids = watch / ".claim" / ".ids"
    ids.mkdir(parents=True)
    res = ids / "github-99"
    res.write_text("", encoding="utf-8")
    now_ts = NOW.timestamp()
    _age_reservation(res, age_s=RESERVATION_GRACE_S + 5.0, now_ts=now_ts)

    err = io.StringIO()
    code = run_trigger(
        "handle-reply",
        ws,
        runner=FakeRunner({("cairn", "run"): RunResult(0, "", "")}),
        cairn_bin="cairn",
        now=NOW,
        err=err,
    )
    assert code == 0
    assert not res.exists()
    assert "orphan reservation github-99" in err.getvalue()


def test_release_orphan_reservations_keeps_live(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    a = _drop(watch, 1, "github", "1", "1")
    assert admit_strict(watch, a).disposition == "admit"
    claim(watch, a)
    # Seed a true aged orphan alongside.
    orphan = watch / ".claim" / ".ids" / "github-orphan"
    orphan.write_text("", encoding="utf-8")
    now_ts = NOW.timestamp()
    _age_reservation(orphan, age_s=RESERVATION_GRACE_S + 1.0, now_ts=now_ts)
    diags = release_orphan_reservations(watch, now=now_ts)
    assert any("github-orphan" in d for d in diags)
    assert reservation_path(watch, "github-1").is_file()  # still live
    assert not reservation_path(watch, "github-orphan").exists()


# --------------------------------------------------------------------------- #
# identity:off equivalence (D7)
# --------------------------------------------------------------------------- #


def test_identity_off_byte_identical_to_pre_t12(tmp_path, monkeypatch):
    """Default trigger (identity:off) claims arbitrary names — same as T11 equivalence."""
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
        """,
    )
    _stub_mint(monkeypatch, ws)
    watch_abs = ws / "inbox" / "replies"
    watch_abs.mkdir(parents=True)
    for name in ("c.json", "a.json", "b.json"):
        (watch_abs / name).write_text(name, encoding="utf-8")

    runner = FakeRunner({("cairn", "run"): RunResult(0, "", "")})
    code = run_trigger("handle-reply", ws, runner=runner, cairn_bin="cairn", now=NOW)
    assert code == 0
    claimed_names = []
    for call in runner.calls:
        argv = call["argv"]
        param = next(a for a in argv if a.startswith("event="))
        claimed_names.append(Path(param.split("=", 1)[1]).name)
    assert claimed_names == ["a.json", "b.json", "c.json"]
    assert (watch_abs / ".done" / "a.json").is_file()
    assert (watch_abs / ".done" / "b.json").is_file()
    assert (watch_abs / ".done" / "c.json").is_file()
    # No identity machinery engaged.
    assert not (watch_abs / ".claim" / ".ids").exists() or not any(
        (watch_abs / ".claim" / ".ids").iterdir()
    )
    assert not (watch_abs / ".rejected").exists()
    t = load_triggers(ws)["handle-reply"]
    assert t.identity == "off"


def test_identity_off_does_not_reject_non_grammar_names(tmp_path, monkeypatch):
    ws = _workspace(
        tmp_path,
        """
        handle-reply:
          pipeline: handle-reply
          watch: inbox/replies
          identity: off
        """,
    )
    _stub_mint(monkeypatch, ws)
    watch = ws / "inbox" / "replies"
    watch.mkdir(parents=True)
    (watch / "whatever.json").write_text("payload", encoding="utf-8")
    code = run_trigger(
        "handle-reply",
        ws,
        runner=FakeRunner({("cairn", "run"): RunResult(0, "", "")}),
        cairn_bin="cairn",
        now=NOW,
    )
    assert code == 0
    assert (watch / ".done" / "whatever.json").is_file()
    assert not (watch / ".rejected").exists()


# --------------------------------------------------------------------------- #
# End-to-end strict drain
# --------------------------------------------------------------------------- #


def test_strict_drain_full_lifecycle(tmp_path, monkeypatch):
    ws = _strict_ws(tmp_path)
    _stub_mint(monkeypatch, ws)
    watch = ws / "inbox" / "replies"
    watch.mkdir(parents=True)
    _drop(watch, 1, "github", "7", "1")

    err = io.StringIO()
    code = run_trigger(
        "handle-reply",
        ws,
        runner=FakeRunner({("cairn", "run"): RunResult(0, "", "")}),
        cairn_bin="cairn",
        now=NOW,
        err=err,
    )
    assert code == 0
    assert (watch / ".done" / "p1-github-7-r1.json").is_file()
    assert (watch / ".done" / "tombstones" / "github-7-r1").is_file()
    assert not reservation_path(watch, "github-7").exists()


def test_strict_drain_rejects_nonconforming_continues(tmp_path, monkeypatch):
    ws = _strict_ws(tmp_path)
    _stub_mint(monkeypatch, ws)
    watch = ws / "inbox" / "replies"
    watch.mkdir(parents=True)
    (watch / "bad.json").write_text("{}", encoding="utf-8")
    _drop(watch, 1, "github", "1", "1")

    err = io.StringIO()
    code = run_trigger(
        "handle-reply",
        ws,
        runner=FakeRunner({("cairn", "run"): RunResult(0, "", "")}),
        cairn_bin="cairn",
        now=NOW,
        err=err,
    )
    assert code == 0
    assert (watch / ".rejected" / "bad.json").is_file()
    assert (watch / ".done" / "p1-github-1-r1.json").is_file()
    assert "rejected" in err.getvalue()


def test_scan_candidates_excludes_rejected_and_deferred(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    (watch / "ok.json").write_text("x", encoding="utf-8")
    (watch / ".rejected").mkdir()
    (watch / ".rejected" / "nope.json").write_text("y", encoding="utf-8")
    (watch / ".deferred").mkdir()
    (watch / ".deferred" / "id").write_text("z", encoding="utf-8")
    found = scan_candidates(watch, "*.json")
    assert [p.name for p in found] == ["ok.json"]


def test_default_max_item_bytes_is_1mib():
    assert DEFAULT_MAX_ITEM_BYTES == 1_048_576


# --------------------------------------------------------------------------- #
# C2 — promote before release (no double-admit window)
# --------------------------------------------------------------------------- #


def test_promote_before_release_deferred_lands_reservation_gone(tmp_path):
    """Terminal retire: deferred newer rev is in inbox AND reservation released."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    a = _drop(watch, 1, "github", "42", "10")
    assert admit_strict(watch, a).disposition == "admit"
    claimed = claim(watch, a)
    assert claimed is not None
    b = _drop(watch, 1, "github", "42", "20")
    assert admit_strict(watch, b).disposition == "defer"

    retire(watch, claimed, outcome=_done(), on_done="done", exit_code=0)
    assert (watch / "p1-github-42-r20.json").is_file()
    assert not reservation_path(watch, "github-42").exists()
    assert not (watch / ".deferred" / "github-42").exists()


def test_promote_while_reservation_held_blocks_concurrent_admit(tmp_path):
    """C2: between promote and release the reservation still covers the identity.

    Simulates the load-bearing order without sleeps: promote first (reservation
    still held) → concurrent admit of another rev must DEFER; only after release
    can a fresh admit succeed. Proves no double-admit interleaving window.
    """
    watch = tmp_path / "inbox"
    watch.mkdir()
    a = _drop(watch, 1, "github", "42", "10")
    assert admit_strict(watch, a).disposition == "admit"
    claimed = claim(watch, a)
    assert claimed is not None
    # Park newer deferred while live.
    b = _drop(watch, 1, "github", "42", "20")
    assert admit_strict(watch, b).disposition == "defer"
    item = parse_item_name(claimed.name)
    assert item is not None

    # Step 1 only: promote under held reservation (plan T1 transfer).
    diag = promote_deferred(watch, item)
    assert diag is not None and "promoted" in diag
    assert (watch / "p1-github-42-r20.json").is_file()
    assert reservation_path(watch, "github-42").is_file()  # STILL held

    # Concurrent admit of yet another rev while reservation held → defer, not admit.
    c = _drop(watch, 1, "github", "42", "30")
    r = admit_strict(watch, c)
    assert r.disposition == "defer"
    assert reservation_path(watch, "github-42").is_file()

    # Step 2: release — only now is the identity free.
    release_reservation(watch, "github-42")
    assert not reservation_path(watch, "github-42").exists()

    # Promoted inbox item can now reserve (single admission path).
    promoted = watch / "p1-github-42-r20.json"
    r2 = admit_strict(watch, promoted)
    assert r2.disposition == "admit"
    assert reservation_path(watch, "github-42").is_file()


def test_release_identity_on_terminal_orders_promote_then_release(tmp_path):
    """release_identity_on_terminal promotes first then drops reservation."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    a = _drop(watch, 1, "github", "7", "1")
    assert admit_strict(watch, a).disposition == "admit"
    claimed = claim(watch, a)
    _drop(watch, 1, "github", "7", "2")
    assert admit_strict(watch, watch / "p1-github-7-r2.json").disposition == "defer"

    diags = release_identity_on_terminal(watch, claimed.name)
    assert any("promoted" in d for d in diags)
    assert (watch / "p1-github-7-r2.json").is_file()
    assert not reservation_path(watch, "github-7").exists()


# --------------------------------------------------------------------------- #
# I1 — numeric fail-safe rev compare
# --------------------------------------------------------------------------- #


def test_rev_order_numeric_10_gt_9():
    assert rev_order("10", "9") == 1
    assert rev_order("9", "10") == -1
    assert rev_order("10", "10") == 0


def test_rev_order_non_numeric_incomparable():
    assert rev_order("a1", "b2") is None
    assert rev_order("10", "a1") is None
    assert rev_order("a1", "10") is None


def test_tombstone_numeric_rev10_admits_after_rev9(tmp_path):
    """I1: rev 10 is newer than tombstoned 9 — admits (lexical would wrongly skip)."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    a = _drop(watch, 1, "github", "42", "9")
    assert admit_strict(watch, a).disposition == "admit"
    claimed = claim(watch, a)
    retire(watch, claimed, outcome=_done(), on_done="done", exit_code=0)
    assert (watch / ".done" / "tombstones" / "github-42-r9").is_file()

    b = _drop(watch, 1, "github", "42", "10")
    r = admit_strict(watch, b)
    assert r.disposition == "admit"
    assert b.is_file() or reservation_path(watch, "github-42").is_file()


def test_tombstone_non_numeric_rev_fails_safe_admits(tmp_path):
    """I1: non-numeric rev pair cannot be ordered → admit (never skip/delete)."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    a = _drop(watch, 1, "github", "42", "a1")
    assert admit_strict(watch, a).disposition == "admit"
    claimed = claim(watch, a)
    retire(watch, claimed, outcome=_done(), on_done="done", exit_code=0)

    b = _drop(watch, 1, "github", "42", "b2")
    r = admit_strict(watch, b)
    assert r.disposition == "admit"  # fail-safe: not skipped


def test_deferred_numeric_10_wins_over_9(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    a = _drop(watch, 1, "github", "42", "1")
    assert admit_strict(watch, a).disposition == "admit"
    claim(watch, a)

    _drop(watch, 1, "github", "42", "9")
    assert admit_strict(watch, watch / "p1-github-42-r9.json").disposition == "defer"
    _drop(watch, 1, "github", "42", "10")
    assert admit_strict(watch, watch / "p1-github-42-r10.json").disposition == "defer"
    deferred = json.loads((watch / ".deferred" / "github-42").read_text(encoding="utf-8"))
    assert deferred["rev"] == "10"  # numeric win, not lexical "9" > "10"


# --------------------------------------------------------------------------- #
# I2 — hostile deeply-nested JSON quarantined
# --------------------------------------------------------------------------- #


def test_deeply_nested_json_quarantined_to_rejected(tmp_path):
    """I2: RecursionError from nested JSON → .rejected/, not left to re-hazard."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    # Deep nesting under the byte cap; exceeds Python's default recursion (~1000).
    depth = 2000
    payload = "[" * depth + "]" * depth
    assert len(payload) < DEFAULT_MAX_ITEM_BYTES
    cand = watch / "p1-github-1-r1.json"
    cand.write_text(payload, encoding="utf-8")

    r = admit_strict(watch, cand)
    assert r.disposition == "reject"
    assert not cand.exists()
    assert (watch / ".rejected" / "p1-github-1-r1.json").is_file()
    assert "nested" in (r.diagnostic or "").lower() or "RecursionError" in (
        r.diagnostic or ""
    )


# --------------------------------------------------------------------------- #
# W4 SG2 — receipt-checked deferred promotion
# --------------------------------------------------------------------------- #


def _failed() -> RunOutcome:
    return RunOutcome(outcome=OutcomeClass.FAILED)


def _validated_run(runs_root: Path, run_id: str, *, receipt_rev: str | None = None) -> Path:
    """Create a schema-valid run dir; optional delivery-receipt artifact."""
    payload = {
        "run_id": run_id,
        "pipeline": "handle-reply",
        "pipeline_hash": "sha256:abc",
        "cairn_version": "0.1.0",
        "params": {},
        "dims": {},
        "executors": {"default": "stub"},
        "models": {},
        "created_at": "2026-07-14T12:00:00.000Z",
        "status": "running",
        "nodes": {},
    }
    run_dir = create_run(runs_root, run_id, payload)
    if receipt_rev is not None:
        (run_dir / "delivery-receipt.json").write_text(
            json.dumps({"rev": receipt_rev, "checked_rev": receipt_rev}),
            encoding="utf-8",
        )
    return run_dir


def test_read_receipt_rev_from_delivery_receipt_artifact(tmp_path):
    run_dir = _validated_run(tmp_path / "runs", "r1", receipt_rev="20")
    assert read_receipt_rev(run_dir) == "20"


def test_read_receipt_rev_none_when_absent(tmp_path):
    run_dir = _validated_run(tmp_path / "runs", "r2")
    assert read_receipt_rev(run_dir) is None
    assert read_receipt_rev(None) is None
    assert read_receipt_rev(tmp_path / "missing") is None


def test_read_receipt_rev_from_run_json_meta(tmp_path):
    payload = {
        "run_id": "r3",
        "pipeline": "handle-reply",
        "pipeline_hash": "sha256:abc",
        "cairn_version": "0.1.0",
        "params": {},
        "dims": {},
        "executors": {"default": "stub"},
        "models": {},
        "created_at": "2026-07-14T12:00:00.000Z",
        "status": "done",
        "nodes": {},
        "delivered_rev": "33",
    }
    run_dir = create_run(tmp_path / "runs", "r3", payload)
    assert read_receipt_rev(run_dir) == "33"


def test_receipt_promotion_deferred_eq_receipt_dropped(tmp_path):
    """Deferred rev == receipt rev R → already delivered → drop, not promote."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    # Live item rev 10, but the run actually delivered rev 20 (refresh mid-flight).
    a = _drop(watch, 1, "github", "42", "10")
    assert admit_strict(watch, a).disposition == "admit"
    claimed = claim(watch, a)
    assert claimed is not None
    run_dir = _validated_run(runs, "deliver-20", receipt_rev="20")
    write_pointer(pointer_path(claimed.parent, claimed.name), run_dir=run_dir)

    # Park deferred at the same rev the receipt recorded.
    deferred = watch / ".deferred"
    deferred.mkdir()
    (deferred / "github-42").write_text(
        _item_body(1, "github", "42", "20"), encoding="utf-8"
    )

    retire(
        watch,
        claimed,
        outcome=_done(),
        on_done="done",
        exit_code=0,
        run_dir=run_dir,
    )
    # Must NOT promote r20 — receipt already covered it.
    assert not (watch / "p1-github-42-r20.json").exists()
    assert not (deferred / "github-42").exists()
    assert (watch / ".done" / "tombstones" / "github-42-r20").is_file()


def test_receipt_promotion_deferred_gt_receipt_promoted(tmp_path):
    """Deferred rev > receipt R → promote (newer work still pending)."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    a = _drop(watch, 1, "github", "42", "10")
    assert admit_strict(watch, a).disposition == "admit"
    claimed = claim(watch, a)
    run_dir = _validated_run(runs, "deliver-10", receipt_rev="10")
    write_pointer(pointer_path(claimed.parent, claimed.name), run_dir=run_dir)

    deferred = watch / ".deferred"
    deferred.mkdir()
    (deferred / "github-42").write_text(
        _item_body(1, "github", "42", "30"), encoding="utf-8"
    )

    retire(
        watch,
        claimed,
        outcome=_done(),
        on_done="done",
        exit_code=0,
        run_dir=run_dir,
    )
    assert (watch / "p1-github-42-r30.json").is_file()
    assert not (deferred / "github-42").exists()


def test_receipt_promotion_no_receipt_falls_back_to_item_rev(tmp_path):
    """No receipt → compare against item.rev (byte-identical to pre-SG2)."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    a = _drop(watch, 1, "github", "42", "20")
    assert admit_strict(watch, a).disposition == "admit"
    claimed = claim(watch, a)
    run_dir = _validated_run(runs, "no-receipt")  # no delivery-receipt
    write_pointer(pointer_path(claimed.parent, claimed.name), run_dir=run_dir)

    deferred = watch / ".deferred"
    deferred.mkdir()
    # Stale deferred ≤ item.rev → drop (same as test_deferred_stale_rev_dropped_on_retire).
    (deferred / "github-42").write_text(
        _item_body(1, "github", "42", "10"), encoding="utf-8"
    )
    retire(
        watch,
        claimed,
        outcome=_done(),
        on_done="done",
        exit_code=0,
        run_dir=run_dir,
    )
    assert not (deferred / "github-42").exists()
    assert not (watch / "p1-github-42-r10.json").exists()
    assert (watch / ".done" / "tombstones" / "github-42-r10").is_file()


def test_promote_deferred_receipt_rev_kwarg_overrides_item(tmp_path):
    """Direct promote_deferred(receipt_rev=) uses receipt, not item.rev."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    item = parse_item_name("p1-github-42-r10.json")
    assert item is not None
    deferred = watch / ".deferred"
    deferred.mkdir()
    (deferred / "github-42").write_text(
        _item_body(1, "github", "42", "15"), encoding="utf-8"
    )
    # Item rev is 10 but receipt says 20 → deferred 15 ≤ 20 → drop.
    diag = promote_deferred(watch, item, receipt_rev="20")
    assert diag is not None and "dropped" in diag
    assert "receipt" in diag
    assert not (deferred / "github-42").exists()
    assert (watch / ".done" / "tombstones" / "github-42-r15").is_file()


# --------------------------------------------------------------------------- #
# W4 SG3 — identity-safe prepare_failed_retry
# --------------------------------------------------------------------------- #


def _park_failed(
    watch: Path,
    prio: int,
    source: str,
    id_: str,
    rev: str,
    *,
    run_dir: Path,
) -> str:
    """Admit → claim → write pointer → FAILED retire. Returns item name."""
    path = _drop(watch, prio, source, id_, rev)
    assert admit_strict(watch, path).disposition == "admit"
    claimed = claim(watch, path)
    assert claimed is not None
    write_pointer(pointer_path(claimed.parent, claimed.name), run_dir=run_dir)
    retire(
        watch,
        claimed,
        outcome=_failed(),
        on_done="done",
        exit_code=1,
        run_dir=run_dir,
    )
    name = claimed.name
    assert (watch / ".failed" / name).is_file()
    assert pointer_path(watch / ".failed", name).is_file()
    # Terminal release left reservation free + tombstone present.
    assert not reservation_path(watch, f"{source}-{id_}").exists()
    assert (watch / ".done" / "tombstones" / f"{source}-{id_}-r{rev}").is_file()
    return name


def test_retry_failed_strict_free_identity_prepares_resume(tmp_path):
    """FAILED strict item with free identity → .claim/ + reservation + run_dir."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = _validated_run(runs, "retry-free")
    name = _park_failed(watch, 1, "github", "42", "10", run_dir=run_dir)

    result = prepare_failed_retry(watch, name, identity_mode="strict")
    assert isinstance(result, RetryPrepared)
    assert result.run_dir == run_dir
    assert result.identity == "github-42"
    assert result.claim_path == watch / ".claim" / name
    assert result.claim_path.is_file()
    assert not (watch / ".failed" / name).exists()
    assert pointer_path(watch / ".claim", name).is_file()
    assert reservation_path(watch, "github-42").is_file()
    # Tombstone still present — re-drop of same rev still dedupes.
    assert (watch / ".done" / "tombstones" / "github-42-r10").is_file()


def test_retry_failed_identity_owned_by_newer_live_refused(tmp_path):
    """FAILED rev whose identity a newer live rev now owns → refused, no move."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    run_old = _validated_run(runs, "retry-old")
    name = _park_failed(watch, 1, "github", "42", "10", run_dir=run_old)

    # A newer rev admits and claims (identity free after failed terminal).
    newer = _drop(watch, 1, "github", "42", "20")
    assert admit_strict(watch, newer).disposition == "admit"
    claimed_newer = claim(watch, newer)
    assert claimed_newer is not None
    assert reservation_path(watch, "github-42").is_file()

    result = prepare_failed_retry(watch, name, identity_mode="strict")
    assert isinstance(result, RetryRefused)
    assert "owned by a newer/live rev" in result.message
    assert result.identity == "github-42"
    # Failed item untouched; no second claim of the old rev.
    assert (watch / ".failed" / name).is_file()
    assert not (watch / ".claim" / name).exists()


def test_retry_failed_identity_owned_by_deferred_refused(tmp_path):
    """Newer deferred for same identity → refuse retry of older failed rev."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = _validated_run(runs, "retry-def")
    name = _park_failed(watch, 1, "github", "42", "10", run_dir=run_dir)

    # Simulate a stranded deferred (newer) without live reservation.
    deferred = watch / ".deferred"
    deferred.mkdir(exist_ok=True)
    (deferred / "github-42").write_text(
        _item_body(1, "github", "42", "50"), encoding="utf-8"
    )

    result = prepare_failed_retry(watch, name, identity_mode="strict")
    assert isinstance(result, RetryRefused)
    assert "deferred" in result.message
    assert (watch / ".failed" / name).is_file()


def test_retry_failed_identity_owned_by_reservation_refused(tmp_path):
    """Held reservation with no live item still blocks retry."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = _validated_run(runs, "retry-res")
    name = _park_failed(watch, 1, "github", "42", "10", run_dir=run_dir)
    assert reserve_identity(watch, "github-42")

    result = prepare_failed_retry(watch, name, identity_mode="strict")
    assert isinstance(result, RetryRefused)
    assert "reservation held" in result.message
    assert (watch / ".failed" / name).is_file()


def test_retry_failed_non_strict_no_reservation_dance(tmp_path):
    """identity:off FAILED item → re-homes to .claim/ without reservation."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = _validated_run(runs, "retry-off")
    # Non-grammar name (identity:off path).
    name = "event-xyz.json"
    item = watch / name
    item.write_text('{"x": 1}', encoding="utf-8")
    claimed = claim(watch, item)
    assert claimed is not None
    write_pointer(pointer_path(claimed.parent, claimed.name), run_dir=run_dir)
    retire(
        watch,
        claimed,
        outcome=_failed(),
        on_done="done",
        exit_code=1,
        run_dir=run_dir,
    )
    assert (watch / ".failed" / name).is_file()

    result = prepare_failed_retry(watch, name, identity_mode="off")
    assert isinstance(result, RetryPrepared)
    assert result.identity is None
    assert result.claim_path.is_file()
    assert not (watch / ".claim" / ".ids").exists() or not any(
        (watch / ".claim" / ".ids").iterdir()
    )


def test_retry_nonexistent_item_errors(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    result = prepare_failed_retry(watch, "p1-github-1-r1.json", identity_mode="strict")
    assert isinstance(result, RetryError)
    assert "no failed item" in result.message


def test_retry_non_failed_item_errors(tmp_path):
    watch = tmp_path / "inbox"
    watch.mkdir()
    a = _drop(watch, 1, "github", "1", "1")
    assert admit_strict(watch, a).disposition == "admit"
    claimed = claim(watch, a)
    assert claimed is not None
    result = prepare_failed_retry(watch, claimed.name, identity_mode="strict")
    assert isinstance(result, RetryError)
    assert ".failed" in result.message


def test_retry_vs_redrop_tombstone_still_dedupes(tmp_path):
    """Retry is sanctioned; a puller re-drop of the same rev still hits tombstone."""
    watch = tmp_path / "inbox"
    watch.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    run_dir = _validated_run(runs, "retry-vs-drop")
    name = _park_failed(watch, 1, "github", "42", "10", run_dir=run_dir)

    # Retry prepares (sanctioned re-entry).
    prepared = prepare_failed_retry(watch, name, identity_mode="strict")
    assert isinstance(prepared, RetryPrepared)

    # Simulate: retry finishes DONE (re-retire).
    retire(
        watch,
        prepared.claim_path,
        outcome=_done(),
        on_done="done",
        exit_code=0,
        run_dir=run_dir,
    )
    # Puller re-drops the same rev → still deduped (tombstone covers).
    redrop = _drop(watch, 1, "github", "42", "10")
    r = admit_strict(watch, redrop)
    assert r.disposition == "skip"
    assert not redrop.exists()
