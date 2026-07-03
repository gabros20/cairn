"""cairn learnings — aggregate `learn` events across all runs (API.md; TOOLING-AND-GROWTH §7).

Behaviour tests against the public surface (collect_learnings, render_learnings). A learn
event is emitted by the walker as `{event: "learn", node, cycle, data: {note, tag}}` on the
run's trail; this module scans every run dir under the runs root and aggregates them.
"""

from __future__ import annotations

from cairn.kernel.learnkit import Learning, collect_learnings, render_learnings
from cairn.kernel.trail import TrailWriter


def _run(runs_root, run_id, learnings, *, pipeline="brease-rebuild"):
    """Fabricate a run dir with a trail carrying the given learn events.

    `learnings` is a list of (node, note, tag) tuples emitted as learn events, plus a
    run-start bookend so the trail looks real.
    """
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(f'{{"pipeline": "{pipeline}"}}')
    w = TrailWriter(run_dir, run_id)
    w.emit("run-start")
    for node, note, tag in learnings:
        w.emit("learn", node=node, data={"note": note, "tag": tag})
    w.close()
    return run_dir


def test_collects_learn_events_from_a_run(tmp_path):
    _run(tmp_path, "acme-redesign-20260703", [("capture", "sites never idle", "crawl")])

    got = collect_learnings(tmp_path)

    assert len(got) == 1
    learning = got[0]
    assert isinstance(learning, Learning)
    assert learning.run_id == "acme-redesign-20260703"
    assert learning.node == "capture"
    assert learning.note == "sites never idle"
    assert learning.tag == "crawl"
    assert learning.pipeline == "brease-rebuild"


def test_aggregates_across_multiple_runs(tmp_path):
    _run(tmp_path, "acme-20260701", [("capture", "a", "crawl")])
    _run(tmp_path, "beta-20260702", [("build", "b", "next"), ("qa", "c", "next")])

    got = collect_learnings(tmp_path)

    assert {lg.note for lg in got} == {"a", "b", "c"}
    assert {lg.run_id for lg in got} == {"acme-20260701", "beta-20260702"}


def test_ordering_is_deterministic_by_at_then_run_then_seq(tmp_path):
    _run(tmp_path, "zeta-20260701", [("n", "z1", "t"), ("n", "z2", "t")])
    _run(tmp_path, "alpha-20260701", [("n", "a1", "t")])

    got = collect_learnings(tmp_path)
    # Sorted by (at, run_id, seq) — the notes come out in a stable, reproducible order.
    ats = [lg.at for lg in got]
    assert ats == sorted(ats)
    # Within one run the seq order (z1 before z2) is preserved.
    zs = [lg.note for lg in got if lg.note.startswith("z")]
    assert zs == ["z1", "z2"]


def test_tag_filter_keeps_only_matching_events(tmp_path):
    _run(tmp_path, "acme-20260701", [("n", "keep", "crawl"), ("n", "drop", "next")])

    got = collect_learnings(tmp_path, tag="crawl")

    assert [lg.note for lg in got] == ["keep"]


def test_since_filter_is_an_inclusive_date_lower_bound(tmp_path):
    run_dir = tmp_path / "acme-20260703"
    run_dir.mkdir()
    (run_dir / "run.json").write_text('{"pipeline": "p"}')
    w = TrailWriter(run_dir, "acme-20260703")
    # Two learn events with explicit, hand-forged timestamps straddling the since date.
    w.emit("learn", node="n", data={"note": "old", "tag": "t"})
    w.emit("learn", node="n", data={"note": "new", "tag": "t"})
    w.close()
    # Rewrite the trail with controlled `at` values (before/after 2026-07-03).
    lines = (run_dir / "trail.jsonl").read_text().splitlines()
    import json as _json

    evs = [_json.loads(x) for x in lines]
    evs[0]["at"] = "2026-07-02T23:59:00.000Z"
    evs[1]["at"] = "2026-07-03T00:00:01.000Z"
    (run_dir / "trail.jsonl").write_text("\n".join(_json.dumps(e) for e in evs) + "\n")

    got = collect_learnings(tmp_path, since="2026-07-03")

    assert [lg.note for lg in got] == ["new"]


def test_tolerates_junk_dirs_and_missing_trails_with_counted_warnings(tmp_path):
    _run(tmp_path, "good-20260701", [("n", "real", "t")])
    (tmp_path / "not-a-run").mkdir()  # dir with no trail
    (tmp_path / "loose-file.txt").write_text("ignore me")  # a file, not a dir
    # A corrupt trail line must not crash the scan.
    bad = tmp_path / "corrupt-20260701"
    bad.mkdir()
    (bad / "trail.jsonl").write_text("{not json at all\n")

    warnings: list[str] = []
    got = collect_learnings(tmp_path, warnings=warnings)

    assert [lg.note for lg in got] == ["real"]
    assert any("not-a-run" in w for w in warnings)


def test_pipeline_is_none_when_run_json_absent(tmp_path):
    run_dir = tmp_path / "orphan-20260701"
    run_dir.mkdir()
    w = TrailWriter(run_dir, "orphan-20260701")
    w.emit("learn", node="n", data={"note": "x", "tag": "t"})
    w.close()

    got = collect_learnings(tmp_path)

    assert len(got) == 1
    assert got[0].pipeline is None


def test_missing_runs_root_returns_empty_not_crash(tmp_path):
    warnings: list[str] = []
    assert collect_learnings(tmp_path / "nope", warnings=warnings) == []
    assert warnings


def test_render_is_deterministic_and_summarises(tmp_path):
    _run(tmp_path, "acme-20260701", [("capture", "note one", "crawl")])
    text = render_learnings(collect_learnings(tmp_path))

    assert "brease-rebuild/capture" in text
    assert "[crawl]" in text
    assert "note one" in text
    assert "1 learning(s)" in text
    assert render_learnings([]) == "no learnings found"
