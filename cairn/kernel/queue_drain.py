"""queue_drain — drain loop: sweep → scan → claim → mint → spawn → retire.

Process orchestration for one trigger firing (docs/FACTORY-PLAN.md §3 W0.5 / D10,
W1a QTP retire-side T3–T6, W3 T2 bounded lazy admission). Imports trigger_host
(declaration) and queue_ledger (claim engine); is not imported by either sibling.

Ledger functions are looked up as **module globals** on this module (``claim``,
``retire``, ``sweep``, ``scan_candidates``, ``count_by_class``). Tests patch those
names here — there is no facade setattr mirror (T5 review obligation).

W3 admission (FACTORY-PLAN §2 T2 / D6): before each claim the drain re-checks
optional caps (waiting_max / blocked_max / capacity_max / wip_max). A full lane
stops further claims (exit 0 — healthy back pressure, not failure). ``concurrency``
runs a bounded ThreadPool when >1; ``concurrency: 1`` + no caps keeps the serial
path byte-identical to pre-W3. ``order: aged`` priority-ages by mtime (see
:data:`AGE_STEP_SECONDS`).
"""

from __future__ import annotations

import hashlib
import re
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import TextIO

from cairn.kernel.config import load_config
from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.gckit import write_queue_pin
from cairn.kernel.plan import plan as build_plan
from cairn.kernel.proc import Runner
from cairn.kernel.queue_ledger import (
    claim,
    count_by_class,
    pointer_path,
    read_pointer,
    retire,
    scan_candidates,
    sweep,
    unclaim,
    write_pointer,
)
from cairn.kernel.runctl import Minted, Refusal, mint_new
from cairn.kernel.runstate import RunExistsError
from cairn.kernel.trail import format_at
from cairn.kernel.trigger_host import Trigger, load_triggers, watch_dir
from cairn.kernel.types import Finding, OutcomeClass, classify_exit

# Priority aging (order: aged): effective_prio = declared_prio − age_seconds / AGE_STEP_SECONDS.
# Lower effective sorts first. One full priority step per hour of file age, so a p9 item
# parked for 9h sorts ahead of a fresh p1 (9 − 9 = 0 < 1). Names without a p<digit>-
# prefix default to declared_prio=5 (middle of 0–9). Tiebreak: filename lexicographic.
AGE_STEP_SECONDS = 3600.0
_PRIO_RE = re.compile(r"^p([0-9])-")


def run_dir_for_item(
    runs_root: Path,
    trigger_name: str,
    item_name: str,
) -> Path:
    """Deterministic run-dir path for a claimed work item (FACTORY-PLAN T5 / W1a).

    Derivation: ``<runs_root>/<trigger_name>-<item_stem>`` where ``item_stem`` is
    ``Path(item_name).stem`` (the claim basename without its final suffix — so a
    collision-suffixed claim ``one-v2.json`` becomes stem ``one-v2``). Identity/rev
    grammar (``p<prio>-<source>-<id>-r<rev>``) lands with W3 admission; until then
    the stem of the ledger filename is the stable key.

    Re-dropping an identical filename while a prior run dir still exists raises
    :class:`~cairn.kernel.runstate.RunExistsError` at mint (``create_run`` never
    auto-suffixes an explicit ``run_dir``). The drain treats that as a guard-class
    refusal: unclaim the item back to the inbox with a diagnostic — not a stuck
    claim. W3 identity/rev dedupe will admit-skip same-name re-entry cleanly.
    """
    stem = Path(item_name).stem
    return Path(runs_root) / f"{trigger_name}-{stem}"


def _runs_root(workspace_dir: Path) -> Path:
    config = load_config(workspace_dir)
    root = Path(config.workspace.runs_dir)
    return root if root.is_absolute() else Path(workspace_dir) / root


def _pipeline_hash(workspace_dir: Path, pipeline: str) -> str:
    path = Path(workspace_dir) / "pipelines" / f"{pipeline}.yaml"
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def preallocate_run(
    workspace_dir: Path,
    trigger: Trigger,
    claimed_path: Path,
    *,
    now: datetime,
) -> Minted | Refusal:
    """Build the plan and :func:`mint_new` into the deterministic run dir.

    Patchable seam for unit tests that drive the drain with a FakeRunner and do
    not want a full plan/mint. Production always plans + mints.
    """
    workspace_dir = Path(workspace_dir)
    claimed_path = Path(claimed_path)
    runs_root = _runs_root(workspace_dir)
    run_dir = run_dir_for_item(runs_root, trigger.name, claimed_path.name)
    params = {trigger.param: str(claimed_path)}
    p = build_plan(
        workspace_dir,
        trigger.pipeline,
        params,
        now=now,
        headless=True,
    )
    minted = mint_new(
        workspace_dir,
        p,
        now=now,
        pipeline_hash=_pipeline_hash(workspace_dir, trigger.pipeline),
        runs_root=runs_root,
        run_dir=run_dir,
    )
    # Reciprocal gc pin (W1d): local sentinel so routine gc cannot delete a run the
    # queue still owns — even when the gc process cannot see this workspace's ledgers.
    if isinstance(minted, Minted):
        write_queue_pin(
            minted.run_dir,
            trigger=trigger.name,
            item=claimed_path.name,
            pinned_at=format_at(now),
        )
    return minted


def _run_one(
    trigger: Trigger,
    claimed_path: Path,
    run_dir: Path,
    workspace_dir: Path,
    runner: Runner,
    cairn_bin: str,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> tuple[int, int]:
    """Spawn one child ``cairn run`` for a claimed event; return ``(returncode, pid)``.

    ``claimed_path`` is ALWAYS the exact path :func:`claim` returned — including any
    ``-v2`` collision suffix — never reconstructed from the original candidate's
    basename. The child receives ``--run-dir`` so it resumes the preallocated run
    (FACTORY-PLAN T5). Streams are re-emitted to ``out``/``err`` when given
    (schedkit/run_schedule posture).
    """
    argv = [
        cairn_bin,
        "run",
        trigger.pipeline,
        "--headless",
        "--run-dir",
        str(run_dir),
        "--param",
        f"{trigger.param}={claimed_path}",
        "--origin",
        f"trigger:{trigger.name}",
    ]
    handle = runner.spawn(argv, cwd=workspace_dir)
    pid = handle.pid
    # Record pid into the claim pointer before wait returns (T5 / W3 lease prep).
    ptr = pointer_path(claimed_path.parent, claimed_path.name)
    if ptr.is_file():
        try:
            rec = read_pointer(ptr)
            write_pointer(
                ptr,
                run_dir=rec.get("run_dir") or run_dir,
                outcome=rec.get("outcome"),
                exit_code=rec.get("exit_code"),
                child_pid=pid,
            )
        except (OSError, ValueError):
            write_pointer(ptr, run_dir=run_dir, child_pid=pid)
    else:
        write_pointer(ptr, run_dir=run_dir, child_pid=pid)
    result = handle.wait()
    if out is not None and result.stdout:
        out.write(result.stdout)
    if err is not None and result.stderr:
        err.write(result.stderr)
    return result.returncode, pid


def _declared_prio(name: str) -> int:
    """Single-digit prio from ``p<digit>-…`` filename grammar; default 5 if absent."""
    m = _PRIO_RE.match(name)
    return int(m.group(1)) if m else 5


def effective_prio(path: Path, *, now_ts: float) -> float:
    """Aged priority: ``declared_prio − age_seconds / AGE_STEP_SECONDS`` (lower first)."""
    path = Path(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = now_ts
    age = max(0.0, now_ts - mtime)
    return _declared_prio(path.name) - (age / AGE_STEP_SECONDS)


def order_candidates(
    candidates: list[Path],
    order: str,
    *,
    now: datetime,
) -> list[Path]:
    """Return candidates ordered for claim. ``name`` keeps scan lexicographic order."""
    if order == "aged":
        now_ts = now.timestamp()
        return sorted(candidates, key=lambda p: (effective_prio(p, now_ts=now_ts), p.name))
    return list(candidates)


def _has_admit_caps(trigger: Trigger) -> bool:
    """Whether any admission-gating cap is set (inbox_max is puller-only — not a gate)."""
    return any(
        v is not None
        for v in (
            trigger.waiting_max,
            trigger.blocked_max,
            trigger.capacity_max,
            trigger.wip_max,
        )
    )


def _cap_stop_reason(trigger: Trigger, depths: dict[str, int]) -> str | None:
    """If a cap is full NOW, return one diagnostic naming which; else None.

    Cap-check order (first match wins): waiting_max → blocked_max → capacity_max →
    wip_max. inbox_max is never checked here (W4 puller surface).
    """
    if trigger.waiting_max is not None and depths["needs_human"] >= trigger.waiting_max:
        return (
            f"review lane full ({depths['needs_human']} needs-human) — not claiming"
        )
    if trigger.blocked_max is not None and depths["blocked"] >= trigger.blocked_max:
        return f"blocked lane full ({depths['blocked']} blocked) — not claiming"
    if trigger.capacity_max is not None and depths["capacity"] >= trigger.capacity_max:
        return f"capacity lane full ({depths['capacity']} capacity) — not claiming"
    if trigger.wip_max is not None and depths["inflight"] >= trigger.wip_max:
        return f"wip full ({depths['inflight']} inflight) — not claiming"
    return None


def _process_claimed(
    *,
    name: str,
    trigger: Trigger,
    watch_abs: Path,
    candidate: Path,
    claimed: Path,
    workspace_dir: Path,
    runner: Runner,
    cairn_bin: str,
    now: datetime,
    out: TextIO | None,
    err: TextIO | None,
    diag: TextIO,
) -> bool:
    """Mint+spawn+retire one claimed item. Returns True if this candidate failed.

    Guard-refusals unclaim and return False (not a drain failure). Post-claim
    hazards leave a stuck claim and return True. WAITING parks return False.
    """
    try:
        try:
            minted = preallocate_run(workspace_dir, trigger, claimed, now=now)
        except ConfigError as exc:
            # Plan/config failure is a guard-class refusal, not a run outcome.
            unclaim(watch_abs, claimed)
            print(
                f"cairn: trigger {name!r}: candidate {candidate.name!r} mint refused "
                f"(config) — left in place: {exc}",
                file=diag,
            )
            return False
        except RunExistsError as exc:
            # Same-filename re-drop while a prior run dir still exists (I1 / W1a).
            # Guard-class: unclaim back to inbox — not an outcome, not a stuck claim.
            item_name = claimed.name
            unclaim(watch_abs, claimed)
            try:
                runs_root = _runs_root(workspace_dir)
            except Exception:  # noqa: BLE001 — diagnostic only; default runs/
                runs_root = Path(workspace_dir) / "runs"
            existing = run_dir_for_item(runs_root, trigger.name, item_name)
            print(
                f"cairn: trigger {name!r}: candidate {candidate.name!r} mint refused "
                f"(run-dir-exists) — left in place: run dir already exists for this "
                f"item name ({existing}) — earlier run of the same event; W3 dedupe "
                f"will admit-skip; re-drop under a new name or remove the run dir "
                f"({exc})",
                file=diag,
            )
            return False
        if isinstance(minted, Refusal):
            # Not an outcome: put the item back; neither failed nor parked.
            unclaim(watch_abs, claimed)
            print(
                f"cairn: trigger {name!r}: candidate {candidate.name!r} mint refused "
                f"({minted.kind.value}) — left in place: {minted.message}",
                file=diag,
            )
            return False
        run_dir = minted.run_dir
        # Pointer BEFORE spawn (T5).
        write_pointer(
            pointer_path(claimed.parent, claimed.name),
            run_dir=run_dir,
            outcome=None,
            exit_code=None,
            child_pid=None,
        )
        returncode, child_pid = _run_one(
            trigger,
            claimed,
            run_dir,
            workspace_dir,
            runner,
            cairn_bin,
            out=out,
            err=err,
        )
        outcome = classify_exit(returncode)
        retire(
            watch_abs,
            claimed,
            outcome=outcome,
            on_done=trigger.on_done,
            exit_code=returncode,
            child_pid=child_pid,
            run_dir=run_dir,
        )
    except Exception as exc:
        # Spawn / retire hazard after claim — stuck claim, not silent retry.
        print(
            f"cairn: trigger {name!r}: candidate {candidate.name!r} hazarded and was "
            f"left in .claim/ as a stuck claim: {exc}",
            file=diag,
        )
        return True
    return outcome.outcome is OutcomeClass.FAILED


def _claim_one(
    *,
    name: str,
    watch_abs: Path,
    candidate: Path,
    diag: TextIO,
) -> tuple[Path | None, bool]:
    """Attempt claim. Returns ``(claimed_or_None, claim_hazarded)``.

    ``claimed is None`` and not hazarded = lost race (benign skip).
    """
    try:
        claimed = claim(watch_abs, candidate)
    except CairnError as exc:
        print(
            f"cairn: trigger {name!r}: candidate {candidate.name!r} hazarded and was "
            f"left in place: {exc}",
            file=diag,
        )
        return None, True
    return claimed, False


def _drain_serial(
    *,
    name: str,
    trigger: Trigger,
    watch_abs: Path,
    candidates: list[Path],
    workspace_dir: Path,
    runner: Runner,
    cairn_bin: str,
    now: datetime,
    out: TextIO | None,
    err: TextIO | None,
    diag: TextIO,
    check_caps: bool,
) -> bool:
    """Serial admitter: claim one at a time, re-checking caps before each claim.

    Returns whether any candidate failed. ``concurrency: 1`` path — when
    ``check_caps`` is False this is the pre-W3 loop body (byte-identical control flow).
    """
    any_failed = False
    for candidate in candidates:
        if check_caps:
            depths = count_by_class(watch_abs, glob=trigger.glob)
            reason = _cap_stop_reason(trigger, depths)
            if reason is not None:
                print(f"cairn: trigger {name!r}: {reason}", file=diag)
                break
        claimed, hazarded = _claim_one(
            name=name, watch_abs=watch_abs, candidate=candidate, diag=diag
        )
        if hazarded:
            any_failed = True
            continue
        if claimed is None:
            continue  # lost the claim race to a concurrent firing — not our event
        failed = _process_claimed(
            name=name,
            trigger=trigger,
            watch_abs=watch_abs,
            candidate=candidate,
            claimed=claimed,
            workspace_dir=workspace_dir,
            runner=runner,
            cairn_bin=cairn_bin,
            now=now,
            out=out,
            err=err,
            diag=diag,
        )
        if failed:
            any_failed = True
    return any_failed


def _drain_pooled(
    *,
    name: str,
    trigger: Trigger,
    watch_abs: Path,
    candidates: list[Path],
    workspace_dir: Path,
    runner: Runner,
    cairn_bin: str,
    now: datetime,
    out: TextIO | None,
    err: TextIO | None,
    diag: TextIO,
    check_caps: bool,
) -> bool:
    """Bounded pool: claim→submit lazily; at most ``concurrency`` children in flight.

    Caps are re-checked on the admitter thread before each new claim (as slots free).
    Per-candidate hazard isolation and lost-race discipline are preserved.
    """
    any_failed = False
    lock = threading.Lock()
    stop_admit = False
    cand_iter = iter(candidates)
    pending: set[Future[bool]] = set()

    def mark_failed() -> None:
        nonlocal any_failed
        with lock:
            any_failed = True

    def try_admit() -> bool:
        """Claim at most one candidate into the pool. Returns True if a future was added."""
        nonlocal stop_admit
        if stop_admit or len(pending) >= trigger.concurrency:
            return False
        if check_caps:
            depths = count_by_class(watch_abs, glob=trigger.glob)
            # In-flight pool workers are already in .claim/ (counted as claimed/inflight).
            reason = _cap_stop_reason(trigger, depths)
            if reason is not None:
                print(f"cairn: trigger {name!r}: {reason}", file=diag)
                stop_admit = True
                return False
        try:
            candidate = next(cand_iter)
        except StopIteration:
            stop_admit = True
            return False
        claimed, hazarded = _claim_one(
            name=name, watch_abs=watch_abs, candidate=candidate, diag=diag
        )
        if hazarded:
            mark_failed()
            return True  # consumed a candidate slot in the iterator sense; try more
        if claimed is None:
            return True  # lost race — keep admitting
        fut = pool.submit(
            _process_claimed,
            name=name,
            trigger=trigger,
            watch_abs=watch_abs,
            candidate=candidate,
            claimed=claimed,
            workspace_dir=workspace_dir,
            runner=runner,
            cairn_bin=cairn_bin,
            now=now,
            out=out,
            err=err,
            diag=diag,
        )
        pending.add(fut)
        return True

    with ThreadPoolExecutor(max_workers=trigger.concurrency) as pool:
        # Seed up to concurrency claims (lazy — only claim when a worker slot exists).
        while len(pending) < trigger.concurrency and not stop_admit:
            if not try_admit():
                if stop_admit:
                    break
                # try_admit returned False only on stop or concurrency full.
                break
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    failed = fut.result()
                except Exception as exc:  # noqa: BLE001 — worker bug; isolate
                    mark_failed()
                    print(
                        f"cairn: trigger {name!r}: pooled worker hazarded: {exc}",
                        file=diag,
                    )
                    continue
                if failed:
                    mark_failed()
            # As slots free, re-check caps and claim the next candidates.
            while len(pending) < trigger.concurrency and not stop_admit:
                if not try_admit():
                    break

    return any_failed


def run_trigger(
    name: str,
    workspace_dir: Path,
    *,
    runner: Runner,
    cairn_bin: str,
    now: datetime,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Drain trigger ``name``: sweep waiting first, then claim+mint+spawn+retire.

    Exit code: ``0`` when every candidate was processed clean (including waiting-class
    parks — they do NOT set failure), OR the scan was empty, OR admission stopped on a
    full cap (healthy back pressure); nonzero when ANY candidate failed (FAILED
    outcome) or a claim/spawn/retire hazard raised. Guard-refusals from mint are not
    outcomes: item returns to the inbox, diagnostic only, neither failed nor parked
    (FACTORY-PLAN T6).

    Per-candidate isolation preserved: a hazard on one event never aborts the drain;
    post-claim exceptions leave stuck claims in ``.claim/``.

    W3: optional caps gate admission (lazy, re-checked before each claim);
    ``concurrency > 1`` runs a bounded pool; ``concurrency: 1`` + no caps keeps the
    serial path (D7 byte-compatible with pre-W3).
    """
    workspace_dir = Path(workspace_dir)
    triggers = load_triggers(workspace_dir)
    if name not in triggers:
        raise ConfigError(
            f"no trigger named {name!r} in triggers.yaml "
            f"(declared: {', '.join(sorted(triggers)) or '(none)'})",
            findings=[Finding("error", f"unknown trigger {name!r}")],
        )
    trigger = triggers[name]
    watch_abs = watch_dir(trigger, workspace_dir)
    diag = err if err is not None else sys.stderr
    any_failed = False

    # T6: sweep FIRST — advance .waiting/ from trail evidence before new claims.
    try:
        report = sweep(watch_abs, on_done=trigger.on_done)
    except Exception as exc:  # noqa: BLE001 — sweep must never abort the drain
        any_failed = True
        print(
            f"cairn: trigger {name!r}: sweep hazarded: {exc}",
            file=diag,
        )
        report = None
    if report is not None:
        for line in report.diagnostics:
            print(f"cairn: trigger {name!r}: sweep: {line}", file=diag)

    candidates = scan_candidates(watch_abs, trigger.glob)
    candidates = order_candidates(candidates, trigger.order, now=now)
    check_caps = _has_admit_caps(trigger)

    # D7: concurrency:1 stays on the serial path (exact claim order + output).
    # Pool only when concurrency > 1.
    if trigger.concurrency > 1:
        if _drain_pooled(
            name=name,
            trigger=trigger,
            watch_abs=watch_abs,
            candidates=candidates,
            workspace_dir=workspace_dir,
            runner=runner,
            cairn_bin=cairn_bin,
            now=now,
            out=out,
            err=err,
            diag=diag,
            check_caps=check_caps,
        ):
            any_failed = True
    else:
        if _drain_serial(
            name=name,
            trigger=trigger,
            watch_abs=watch_abs,
            candidates=candidates,
            workspace_dir=workspace_dir,
            runner=runner,
            cairn_bin=cairn_bin,
            now=now,
            out=out,
            err=err,
            diag=diag,
            check_caps=check_caps,
        ):
            any_failed = True
    return 1 if any_failed else 0
