"""Unit tests for the batch engine (cairn/kernel/batchkit.py).

batchkit spawns one `cairn run <pipeline> --headless` SUBPROCESS per JSONL line of a
params file, each in its own run dir, with a bounded process pool. These tests inject a
fake `spawn` so the pool logic, ordering, aggregate exit-code rule, and JSONL validation
run fully offline and fast; one test exercises the real `python -m cairn` child on the
scaffolded `hello` pipeline.
"""

from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path

import pytest

from cairn.kernel import batchkit
from cairn.kernel.errors import ConfigError
from cairn.kernel.types import ExitCode


def _write_jsonl(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _ok_spawn(run_dir_for):
    """A fake spawn that always succeeds, echoing a run-complete marker per its params.

    Mirrors the real CLI: a SUCCESS prints its ``→ run_dir`` marker to stdout (cli.py), so
    stderr is empty. Spawn returns ``(exit, stdout, stderr)``.
    """

    def spawn(argv, cwd):
        # derive a stable pseudo run dir from the --param values in argv
        params = _params_of(argv)
        rd = run_dir_for(params)
        return 0, f"cairn: run complete → {rd}\n", ""

    return spawn


def _params_of(argv):
    params = {}
    it = iter(argv)
    for tok in it:
        if tok == "--param":
            k, _, v = next(it).partition("=")
            params[k] = v
    return params


# --------------------------------------------------------------------------- #
# Tracer.
# --------------------------------------------------------------------------- #


def test_run_batch_returns_outcome_per_line_in_input_order(tmp_path):
    pf = _write_jsonl(tmp_path / "sites.jsonl", ['{"url": "a"}', '{"url": "b"}'])
    out = io.StringIO()
    res = batchkit.run_batch(
        tmp_path,
        "hello",
        pf,
        jobs=2,
        spawn=_ok_spawn(lambda p: f"/runs/{p['url']}"),
        out=out,
    )
    assert res.pipeline == "hello"
    assert [o.params["url"] for o in res.outcomes] == ["a", "b"]
    assert res.exit_code == int(ExitCode.OK)
    assert res.ok


# --------------------------------------------------------------------------- #
# Eager JSONL validation — bad input fails before anything spawns.
# --------------------------------------------------------------------------- #


def _never_spawn(argv, cwd):  # pragma: no cover - asserts it is never called
    raise AssertionError("spawn must not run when the params file is invalid")


def test_malformed_json_line_rejected_before_any_spawn(tmp_path):
    pf = _write_jsonl(tmp_path / "sites.jsonl", ['{"url": "a"}', "{not json}"])
    with pytest.raises(ConfigError) as exc:
        batchkit.run_batch(tmp_path, "hello", pf, jobs=2, spawn=_never_spawn)
    assert "2" in str(exc.value)  # names the offending line


def test_non_object_json_line_rejected(tmp_path):
    pf = _write_jsonl(tmp_path / "sites.jsonl", ['{"url": "a"}', '["not", "an", "object"]'])
    with pytest.raises(ConfigError):
        batchkit.run_batch(tmp_path, "hello", pf, jobs=2, spawn=_never_spawn)


def test_empty_params_file_is_config_error(tmp_path):
    pf = (tmp_path / "empty.jsonl")
    pf.write_text("\n  \n\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        batchkit.run_batch(tmp_path, "hello", pf, jobs=2, spawn=_never_spawn)


def test_blank_lines_are_skipped(tmp_path):
    pf = (tmp_path / "sites.jsonl")
    pf.write_text('{"url": "a"}\n\n{"url": "b"}\n', encoding="utf-8")
    res = batchkit.run_batch(
        tmp_path, "hello", pf, jobs=2, spawn=_ok_spawn(lambda p: f"/runs/{p['url']}"), out=io.StringIO()
    )
    assert [o.params["url"] for o in res.outcomes] == ["a", "b"]


def test_jobs_must_be_at_least_one(tmp_path):
    pf = _write_jsonl(tmp_path / "sites.jsonl", ['{"url": "a"}'])
    with pytest.raises(ConfigError):
        batchkit.run_batch(tmp_path, "hello", pf, jobs=0, spawn=_never_spawn)


# --------------------------------------------------------------------------- #
# Aggregate exit-code rule + failure policy.
# --------------------------------------------------------------------------- #


def _coded_spawn(codes_by_url):
    """Fake spawn mirroring the real CLI's stream split: a SUCCESS marker goes to stdout, a
    FAILURE (``run halted``) marker goes to stderr (cli.py). Returns ``(exit, stdout, stderr)``."""

    def spawn(argv, cwd):
        p = _params_of(argv)
        code = codes_by_url[p["url"]]
        marker = f"cairn: run halted (exit {code}) → /runs/{p['url']}\n"
        if code == 0:
            return 0, f"cairn: run complete → /runs/{p['url']}\n", ""
        return code, "", marker

    return spawn


def test_aggregate_is_ok_only_when_all_runs_succeed(tmp_path):
    pf = _write_jsonl(tmp_path / "s.jsonl", ['{"url": "a"}', '{"url": "b"}'])
    res = batchkit.run_batch(
        tmp_path, "hello", pf, jobs=2, out=io.StringIO(),
        spawn=_coded_spawn({"a": 0, "b": 0}),
    )
    assert res.exit_code == 0


def test_aggregate_is_highest_failing_code(tmp_path):
    # CONFIG=2, EXECUTOR=4, NEEDS_HUMAN=6 → aggregate = 6 (highest escalation)
    pf = _write_jsonl(tmp_path / "s.jsonl", ['{"url": "a"}', '{"url": "b"}', '{"url": "c"}'])
    res = batchkit.run_batch(
        tmp_path, "hello", pf, jobs=3, out=io.StringIO(),
        spawn=_coded_spawn({"a": 2, "b": 6, "c": 4}),
    )
    assert res.exit_code == int(ExitCode.NEEDS_HUMAN) == 6
    assert not res.ok


def test_failing_run_does_not_cancel_siblings(tmp_path):
    # every line runs even though one fails: all three outcomes present, order preserved
    pf = _write_jsonl(tmp_path / "s.jsonl", ['{"url": "a"}', '{"url": "b"}', '{"url": "c"}'])
    res = batchkit.run_batch(
        tmp_path, "hello", pf, jobs=1, out=io.StringIO(),
        spawn=_coded_spawn({"a": 0, "b": 4, "c": 0}),
    )
    assert [o.params["url"] for o in res.outcomes] == ["a", "b", "c"]
    assert [o.exit_code for o in res.outcomes] == [0, 4, 0]
    assert res.total == 3
    assert [o.params["url"] for o in res.failed] == ["b"]


# --------------------------------------------------------------------------- #
# Per-run dir collection + progress streaming.
# --------------------------------------------------------------------------- #


def test_each_outcome_carries_its_own_run_dir(tmp_path):
    pf = _write_jsonl(tmp_path / "s.jsonl", ['{"url": "a"}', '{"url": "b"}'])
    res = batchkit.run_batch(
        tmp_path, "hello", pf, jobs=2, out=io.StringIO(),
        spawn=_ok_spawn(lambda p: f"/runs/site-{p['url']}"),
    )
    dirs = {o.params["url"]: o.run_dir for o in res.outcomes}
    assert dirs["a"] == Path("/runs/site-a")
    assert dirs["b"] == Path("/runs/site-b")


def test_run_dir_none_when_child_prints_no_marker(tmp_path):
    pf = _write_jsonl(tmp_path / "s.jsonl", ['{"url": "a"}'])
    res = batchkit.run_batch(
        tmp_path, "hello", pf, jobs=1, out=io.StringIO(),
        spawn=lambda argv, cwd: (4, "", "traceback: boom\n"),
    )
    assert res.outcomes[0].run_dir is None
    assert res.outcomes[0].exit_code == 4


def test_awaiting_human_marker_is_parsed(tmp_path):
    out = "cairn: halted awaiting a human → /runs/x  (answer + `cairn resume /runs/x`)\n"
    assert batchkit.parse_run_dir(out) == Path("/runs/x")


def test_parse_run_dir_last_marker_wins():
    # a child can print more than one marker line; the TERMINAL outcome is last
    out = (
        "cairn: already done → /runs/stale\n"
        "some interleaved child output\n"
        "cairn: run complete → /runs/final\n"
    )
    assert batchkit.parse_run_dir(out) == Path("/runs/final")


# --------------------------------------------------------------------------- #
# Spawn crash containment — an exception is a failed outcome, never a fleet abort.
# --------------------------------------------------------------------------- #


def test_spawn_exception_becomes_failed_outcome_not_batch_abort(tmp_path):
    pf = _write_jsonl(tmp_path / "s.jsonl", ['{"url": "a"}', '{"url": "b"}', '{"url": "c"}'])

    def spawn(argv, cwd):
        p = _params_of(argv)
        if p["url"] == "b":
            raise FileNotFoundError("no such workspace dir")
        return 0, f"cairn: run complete → /runs/{p['url']}\n", ""

    out = io.StringIO()
    res = batchkit.run_batch(tmp_path, "hello", pf, jobs=1, out=out, spawn=spawn)

    # siblings kept running: all three outcomes present, input order
    assert [o.params["url"] for o in res.outcomes] == ["a", "b", "c"]
    crashed = res.outcomes[1]
    assert crashed.exit_code == int(ExitCode.EXECUTOR) == 4
    assert crashed.run_dir is None
    assert [o.exit_code for o in res.outcomes] == [0, 4, 0]
    assert res.exit_code == int(ExitCode.EXECUTOR)
    # progress still streamed one line per completion, the crash line naming the exception
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 3
    assert "no such workspace dir" in out.getvalue()


# --------------------------------------------------------------------------- #
# Failed-child stderr tail — the failure reason survives into RunOutcome.error.
# --------------------------------------------------------------------------- #


def test_failed_child_stderr_tail_lands_in_error(tmp_path):
    # A child that starts but exits non-zero: its stderr (the actual failure reason) is
    # retained as a legible tail in RunOutcome.error — the marker is on stderr for failures.
    pf = _write_jsonl(tmp_path / "s.jsonl", ['{"url": "a"}'])

    def spawn(argv, cwd):
        stderr = (
            "cairn: gate 'design' halted: fidelity below threshold\n"
            "cairn: run halted (exit 6) → /runs/a\n"
        )
        return 6, "some normal progress on stdout\n", stderr

    res = batchkit.run_batch(tmp_path, "hello", pf, jobs=1, out=io.StringIO(), spawn=spawn)
    o = res.outcomes[0]
    assert o.exit_code == 6
    assert o.error is not None
    assert "fidelity below threshold" in o.error
    # marker is still parsed off the (stderr) marker line — failure markers go to stderr
    assert o.run_dir == Path("/runs/a")


def test_successful_child_keeps_error_none_even_with_stderr(tmp_path):
    # A run that exits 0 keeps error=None regardless of any stderr chatter (warnings etc.).
    pf = _write_jsonl(tmp_path / "s.jsonl", ['{"url": "a"}'])

    def spawn(argv, cwd):
        return 0, "cairn: run complete → /runs/a\n", "warning: deprecated flag\n"

    res = batchkit.run_batch(tmp_path, "hello", pf, jobs=1, out=io.StringIO(), spawn=spawn)
    assert res.outcomes[0].error is None
    assert res.outcomes[0].ok


def test_error_tail_is_bounded_for_a_runaway_child(tmp_path):
    # A child that floods stderr must not balloon memory: RunOutcome.error keeps only a
    # bounded tail (last N lines, capped in size) — the END of the stream (where the real
    # failure is) survives; the head is dropped.
    pf = _write_jsonl(tmp_path / "s.jsonl", ['{"url": "a"}'])
    flood = "\n".join(f"noise line {i}" for i in range(5000)) + "\nFATAL: the real reason\n"

    def spawn(argv, cwd):
        return 4, "", flood

    res = batchkit.run_batch(tmp_path, "hello", pf, jobs=1, out=io.StringIO(), spawn=spawn)
    err = res.outcomes[0].error
    assert err is not None
    # bounded: far smaller than the multi-MB flood, and both bounds are honored
    assert len(err) <= batchkit._ERROR_TAIL_MAX_BYTES
    assert len(err.splitlines()) <= batchkit._ERROR_TAIL_MAX_LINES
    # the tail (the real reason) is what we keep; the head noise is gone
    assert "FATAL: the real reason" in err
    assert "noise line 0" not in err


def test_failed_child_with_empty_stderr_has_none_error(tmp_path):
    # A silent failure (non-zero exit, no stderr) leaves error=None — the exit code alone
    # is the signal; there is no tail to show.
    pf = _write_jsonl(tmp_path / "s.jsonl", ['{"url": "a"}'])

    def spawn(argv, cwd):
        return 4, "cairn: run complete → /runs/a\n", ""

    res = batchkit.run_batch(tmp_path, "hello", pf, jobs=1, out=io.StringIO(), spawn=spawn)
    assert res.outcomes[0].exit_code == 4
    assert res.outcomes[0].error is None


def test_progress_line_stays_single_line_for_multiline_error(tmp_path):
    # A failed child with a multi-line stderr tail must still stream ONE progress line
    # (the per-completion contract) — the progress compacts the error to its first line.
    pf = _write_jsonl(tmp_path / "s.jsonl", ['{"url": "a"}'])

    def spawn(argv, cwd):
        return 4, "", "first failure line\nsecond line\nthird line\n"

    out = io.StringIO()
    batchkit.run_batch(tmp_path, "hello", pf, jobs=1, out=out, spawn=spawn)
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "first failure line" in lines[0]
    assert "second line" not in lines[0]


# --------------------------------------------------------------------------- #
# Summary determinism — input order even when completion order is reversed.
# --------------------------------------------------------------------------- #


def test_summary_is_input_ordered_despite_reordered_completion(tmp_path):
    pf = _write_jsonl(tmp_path / "s.jsonl", [f'{{"url": "{i}"}}' for i in range(4)])
    completion_order: list[str] = []
    lock = threading.Lock()

    def spawn(argv, cwd):
        p = _params_of(argv)
        # line 0 finishes LAST; later lines finish first
        time.sleep(0.08 if p["url"] == "0" else 0.01 * (int(p["url"]) + 1))
        with lock:
            completion_order.append(p["url"])
        return 0, f"cairn: run complete → /runs/{p['url']}\n", ""

    res = batchkit.run_batch(tmp_path, "hello", pf, jobs=4, out=io.StringIO(), spawn=spawn)

    assert completion_order[-1] == "0"  # completion really was reordered
    assert completion_order != ["0", "1", "2", "3"]
    # summary stays input-ordered regardless
    assert [o.params["url"] for o in res.outcomes] == ["0", "1", "2", "3"]


def test_streams_one_progress_line_per_completion(tmp_path):
    pf = _write_jsonl(tmp_path / "s.jsonl", ['{"url": "a"}', '{"url": "b"}', '{"url": "c"}'])
    out = io.StringIO()
    batchkit.run_batch(
        tmp_path, "hello", pf, jobs=3, out=out,
        spawn=_ok_spawn(lambda p: f"/runs/{p['url']}"),
    )
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 3
    assert lines[-1].startswith("[3/3]")


# --------------------------------------------------------------------------- #
# Pool bounding — never more than `jobs` children concurrently.
# --------------------------------------------------------------------------- #


def test_pool_bounds_concurrency(tmp_path):
    pf = _write_jsonl(tmp_path / "s.jsonl", [f'{{"url": "{i}"}}' for i in range(8)])
    live = 0
    peak = 0
    lock = threading.Lock()

    def spawn(argv, cwd):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.02)
        with lock:
            live -= 1
        p = _params_of(argv)
        return 0, f"cairn: run complete → /runs/{p['url']}\n", ""

    batchkit.run_batch(tmp_path, "hello", pf, jobs=3, out=io.StringIO(), spawn=spawn)
    assert peak <= 3
    assert peak >= 2  # actually parallel, not serialized


# --------------------------------------------------------------------------- #
# Child argv construction.
# --------------------------------------------------------------------------- #


def test_build_run_argv_is_headless_run_with_params_gates_and_passthrough():
    argv = batchkit.build_run_argv(
        "brease-rebuild",
        {"url": "https://x.com", "mode": "redesign"},
        {"scope": "all"},
        ["--to", "blueprint"],
    )
    assert argv[:2] == [sys.executable, "-m"]
    assert argv[2:6] == ["cairn", "run", "brease-rebuild", "--headless"]
    assert "--param" in argv and "url=https://x.com" in argv
    assert "mode=redesign" in argv
    i = argv.index("--gate")
    assert argv[i + 1] == "scope=all"
    assert argv[-2:] == ["--to", "blueprint"]


def test_build_run_argv_stringifies_non_string_params():
    argv = batchkit.build_run_argv("hello", {"n": 3, "flag": True}, {}, [])
    assert "n=3" in argv
    assert "flag=True" in argv


# --------------------------------------------------------------------------- #
# Integration — the real child on the scaffolded hello pipeline.
# --------------------------------------------------------------------------- #


def test_real_child_runs_hello_headless(tmp_path):
    from cairn.kernel import newkit

    ws = newkit.new_workspace("batchws", tmp_path)
    pf = _write_jsonl(ws / "sites.jsonl", ['{"name": "Ada"}', '{"name": "Bo"}'])
    out = io.StringIO()
    res = batchkit.run_batch(ws, "hello", pf, jobs=2, out=out)

    assert res.ok
    assert res.exit_code == 0
    assert res.total == 2
    for o in res.outcomes:
        assert o.exit_code == 0
        assert o.run_dir is not None
        assert (o.run_dir / "run.json").is_file()
    # distinct run dirs — per-run isolation
    assert res.outcomes[0].run_dir != res.outcomes[1].run_dir

