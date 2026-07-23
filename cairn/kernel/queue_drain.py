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

Backpressure caps are soft under concurrency>1: up to ``concurrency`` in-flight
items may land in one class past its cap before the next admission check
observes them (outcome class is unknown until a child retires off the admitter
thread); ``wip_max`` is hard (claim is synchronous on the admitter). This is the
same bounded-overshoot tradeoff FACTORY-PLAN §2 T2 accepts, here bounded by the
pool width rather than the process count.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, TextIO

from cairn.kernel.config import load_config
from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.gckit import write_queue_pin
from cairn.kernel.plan import plan as build_plan
from cairn.kernel.proc import Runner
from cairn.kernel.queue_ledger import (
    DEFAULT_MAX_ITEM_BYTES,
    admit_strict,
    audit_ledger,
    boot_id,
    check_ledger_version,
    claim,
    count_by_class,
    effective_lease_ttl,
    pointer_path,
    read_pointer,
    release_orphan_reservations,
    retire,
    scan_candidates,
    stamp_ledger_version,
    sweep,
    unclaim,
    update_lease_child_pid,
    write_lease,
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
    # Lease child_pid update (T13) — same moment as pointer; no-op if no lease.
    watch_abs = claimed_path.parent.parent  # .../<watch>/.claim/<name>
    try:
        update_lease_child_pid(watch_abs, claimed_path.name, pid)
    except Exception:  # noqa: BLE001 — best-effort; pointer already holds pid
        pass
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
    lease_ttl_s: int | None = None,
    claimed_at: float | None = None,
    current_boot_id: str | None = None,
) -> tuple[Path | None, bool]:
    """Attempt claim. Returns ``(claimed_or_None, claim_hazarded)``.

    ``claimed is None`` and not hazarded = lost race (benign skip).

    When ``lease_ttl_s`` is set (lease-enabled trigger), write the claim lease
    immediately after a successful claim (child_pid filled later on spawn).
    Serial default-concurrency triggers pass ``lease_ttl_s=None`` — no lease
    file, stuck-forever preserved (D7).
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
    if claimed is not None and lease_ttl_s is not None:
        try:
            write_lease(
                watch_abs,
                claimed.name,
                drain_pid=os.getpid(),
                child_pid=None,
                boot_id=current_boot_id if current_boot_id is not None else boot_id(),
                claimed_at=claimed_at if claimed_at is not None else time.time(),
                ttl_s=lease_ttl_s,
            )
        except Exception as exc:  # noqa: BLE001 — lease write failure is a hazard
            print(
                f"cairn: trigger {name!r}: candidate {candidate.name!r} lease write "
                f"hazarded after claim — left in .claim/: {exc}",
                file=diag,
            )
            return claimed, True
    return claimed, False


def _admit_before_claim(
    *,
    name: str,
    trigger: Trigger,
    watch_abs: Path,
    candidate: Path,
    diag: TextIO,
) -> bool:
    """Strict-identity gate before claim. Returns True if the candidate may be claimed.

    ``identity: off`` (default) is a no-op — D7 byte-compatible with pre-T12.
    For ``identity: strict``: envelope + reservation + tombstone dedupe + defer
    (FACTORY-PLAN T1). Non-admit dispositions emit a diagnostic and skip claim.
    """
    if trigger.identity != "strict":
        return True
    max_bytes = (
        trigger.max_item_bytes
        if trigger.max_item_bytes is not None
        else DEFAULT_MAX_ITEM_BYTES
    )
    try:
        result = admit_strict(watch_abs, candidate, max_item_bytes=max_bytes)
    except Exception as exc:  # noqa: BLE001 — per-candidate isolation
        print(
            f"cairn: trigger {name!r}: candidate {candidate.name!r} identity "
            f"admission hazarded: {exc}",
            file=diag,
        )
        return False
    if result.diagnostic:
        print(
            f"cairn: trigger {name!r}: {result.diagnostic}",
            file=diag,
        )
    return result.disposition == "admit"


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
    lease_ttl_s: int | None = None,
    claimed_at: float | None = None,
    current_boot_id: str | None = None,
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
        # T1 identity:strict — envelope/reserve/dedupe/defer BEFORE claim.
        if not _admit_before_claim(
            name=name,
            trigger=trigger,
            watch_abs=watch_abs,
            candidate=candidate,
            diag=diag,
        ):
            continue
        claimed, hazarded = _claim_one(
            name=name,
            watch_abs=watch_abs,
            candidate=candidate,
            diag=diag,
            lease_ttl_s=lease_ttl_s,
            claimed_at=claimed_at,
            current_boot_id=current_boot_id,
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
    lease_ttl_s: int | None = None,
    claimed_at: float | None = None,
    current_boot_id: str | None = None,
) -> bool:
    """Bounded pool: claim→submit lazily; at most ``concurrency`` children in flight.

    Caps are re-checked on the admitter thread before each new claim (as slots free).
    Per-candidate hazard isolation and lost-race discipline are preserved: a
    worker that raises is marked failed and siblings still complete; a
    KeyboardInterrupt during the drain blocks on ``ThreadPoolExecutor``
    shutdown until in-flight ``handle.wait()`` calls return (acceptable —
    no orphaned children; longer perceived hang than the serial path's
    single in-flight child).

    Soft-cap overshoot (waiting_max / blocked_max / capacity_max): up to
    ``concurrency`` in-flight items may retire into one class past its cap
    before the next ``try_admit`` observes them; ``wip_max`` is hard because
    claim updates ``.claim/`` on the admitter thread. Bound is the pool width
    (FACTORY-PLAN §2 T2 tradeoff).
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
        # T1 identity:strict — envelope/reserve/dedupe/defer BEFORE claim.
        if not _admit_before_claim(
            name=name,
            trigger=trigger,
            watch_abs=watch_abs,
            candidate=candidate,
            diag=diag,
        ):
            return True  # consumed candidate (rejected/skipped/deferred); keep admitting
        claimed, hazarded = _claim_one(
            name=name,
            watch_abs=watch_abs,
            candidate=candidate,
            diag=diag,
            lease_ttl_s=lease_ttl_s,
            claimed_at=claimed_at,
            current_boot_id=current_boot_id,
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
    post-claim exceptions leave stuck claims in ``.claim/``. On the pooled path a
    worker exception is isolated the same way (siblings still retire).

    W3: optional caps gate admission (lazy, re-checked before each claim);
    ``concurrency > 1`` runs a bounded pool; ``concurrency: 1`` + no caps keeps the
    serial path (D7 byte-compatible with pre-W3).

    Backpressure caps are soft under concurrency>1: up to ``concurrency``
    in-flight items may land in one class past its cap before the next admission
    check observes them; ``wip_max`` is hard. This is the same bounded-overshoot
    tradeoff FACTORY-PLAN §2 T2 accepts, here bounded by the pool width rather
    than the process count. A subsequent drain re-checks live depths and refuses
    further claims once the class is at or over cap. KeyboardInterrupt during a
    pooled drain blocks on pool shutdown until in-flight children return.
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
    now_ts = now.timestamp()
    lease_ttl = effective_lease_ttl(trigger.lease_ttl_s, trigger.concurrency)
    cur_boot = boot_id()

    # T8: refuse a newer-than-us ledger; stamp/adopt older-or-absent on proceed.
    check_ledger_version(watch_abs)
    stamp_ledger_version(watch_abs)

    # T6/T13: sweep FIRST — lease reap + advance .waiting/ + mop stranded deferred.
    try:
        report = sweep(
            watch_abs,
            on_done=trigger.on_done,
            now=now_ts,
            lease_ttl_s=lease_ttl,
            current_boot_id=cur_boot,
        )
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

    # T1: release orphan reservations (reserve-without-claim crash window) before
    # new admits. Grace-gated (RESERVATION_GRACE_S) so a concurrent drain cannot
    # free a live reserve→claim gap. Cheap no-op when no .ids/ present.
    try:
        for line in release_orphan_reservations(watch_abs, now=now_ts):
            print(f"cairn: trigger {name!r}: {line}", file=diag)
    except Exception as exc:  # noqa: BLE001 — never abort the drain
        print(
            f"cairn: trigger {name!r}: orphan-reservation sweep hazarded: {exc}",
            file=diag,
        )

    candidates = scan_candidates(watch_abs, trigger.glob)
    candidates = order_candidates(candidates, trigger.order, now=now)
    check_caps = _has_admit_caps(trigger)

    # D7: concurrency:1 stays on the serial path (exact claim order + output).
    # Pool only when concurrency > 1. Leases off on serial default (no lease key).
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
            lease_ttl_s=lease_ttl,
            claimed_at=now_ts,
            current_boot_id=cur_boot,
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
            lease_ttl_s=lease_ttl,
            claimed_at=now_ts,
            current_boot_id=cur_boot,
        ):
            any_failed = True
    return 1 if any_failed else 0


# --------------------------------------------------------------------------- #
# factory reconcile — single-flight all-trigger health pass (T13 / D1)
# --------------------------------------------------------------------------- #

RECONCILE_LOCK_NAME = ".cairn-reconcile.lock"


@dataclass(frozen=True)
class TriggerReconcileSummary:
    """One-line-per-trigger outcome of :func:`reconcile_workspace`."""

    name: str
    waiting: int
    blocked: int
    capacity: int
    needs_human: int
    reaped: int
    flagged_live: int
    promoted_deferred: int
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconcileReport:
    """Workspace-level reconcile outcome."""

    summaries: tuple[TriggerReconcileSummary, ...]
    already_running: bool = False
    hazarded: bool = False


@contextmanager
def _reconcile_lock(workspace_dir: Path) -> Iterator[bool]:
    """Advisory non-blocking flock on a workspace-level lock file.

    Yields ``True`` when this process holds the lock, ``False`` when another
    reconcile is already running (contention → exit 0, "already running").
    Mirrors :func:`cairn.kernel.runstate.run_lock` (flock, non-blocking).
    """
    workspace_dir = Path(workspace_dir)
    lock_path = workspace_dir / RECONCILE_LOCK_NAME
    lock_path.touch(exist_ok=True)
    fh = lock_path.open("r+", encoding="utf-8")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        try:
            yield True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        if not fh.closed:
            fh.close()


def reconcile_workspace(
    workspace_dir: Path,
    *,
    now: datetime,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> ReconcileReport:
    """Single-flight health pass over every declared trigger (T13 / D1).

    No daemon: host-woken or manual. For each trigger:
    - release aged orphan reservations
    - sweep (lease reap + waiting advance + stranded-deferred mop)
    - report one-line summary (waiting/blocked/capacity/reaped)

    Contention on the workspace reconcile lock → ``already_running=True``
    (caller exits 0). Real hazards set ``hazarded=True`` (nonzero exit).

    **Concurrency (I1 / T13 r1):** the flock serializes reconcile-vs-reconcile
    only. Drain-vs-reconcile sweep overlap is accepted and safe by construction
    (T6 lost-race ledger moves; C1 drain_pid liveness so a live claim→spawn
    window is never reaped; reaped runs park in ``.waiting/`` and resume under
    T6's nonblocking run-lock — no double-drive). No cross-process admission
    lock is added (D-doctrine).
    """
    workspace_dir = Path(workspace_dir)
    diag = err if err is not None else sys.stderr
    out_s = out if out is not None else sys.stdout
    now_ts = now.timestamp()
    cur_boot = boot_id()

    with _reconcile_lock(workspace_dir) as held:
        if not held:
            print("cairn: reconcile already running", file=out_s)
            return ReconcileReport(summaries=(), already_running=True)

        triggers = load_triggers(workspace_dir)
        summaries: list[TriggerReconcileSummary] = []
        hazarded = False

        for name in sorted(triggers):
            trigger = triggers[name]
            watch_abs = watch_dir(trigger, workspace_dir)
            lease_ttl = effective_lease_ttl(trigger.lease_ttl_s, trigger.concurrency)
            reaped_n = 0
            flagged_n = 0
            promoted_n = 0
            diags: list[str] = []

            # T8: refuse newer ledger (loud); stamp older/absent.
            try:
                check_ledger_version(watch_abs)
                stamp_ledger_version(watch_abs)
            except ConfigError as exc:
                hazarded = True
                diags.append(f"ledger-version refused: {exc}")
                summary = TriggerReconcileSummary(
                    name=name,
                    waiting=0,
                    blocked=0,
                    capacity=0,
                    needs_human=0,
                    reaped=0,
                    flagged_live=0,
                    promoted_deferred=0,
                    diagnostics=tuple(diags),
                )
                summaries.append(summary)
                print(f"cairn: reconcile {name}: LEDGER-VERSION REFUSED: {exc}", file=diag)
                continue

            try:
                for line in release_orphan_reservations(watch_abs, now=now_ts):
                    diags.append(line)
            except Exception as exc:  # noqa: BLE001
                hazarded = True
                diags.append(f"orphan-reservation sweep hazarded: {exc}")

            try:
                report = sweep(
                    watch_abs,
                    on_done=trigger.on_done,
                    now=now_ts,
                    lease_ttl_s=lease_ttl,
                    current_boot_id=cur_boot,
                )
                reaped_n = len(report.reaped)
                flagged_n = len(report.flagged_live)
                promoted_n = len(report.promoted_deferred)
                diags.extend(report.diagnostics)
            except Exception as exc:  # noqa: BLE001
                hazarded = True
                diags.append(f"sweep hazarded: {exc}")

            # T8 audit AFTER repair passes so residual violations are what surface
            # (sweep/orphan release may have fixed transient claim↔pointer gaps).
            # Surface + flag; do NOT auto-delete (over-preserve).
            try:
                for issue in audit_ledger(watch_abs):
                    diags.append(f"audit: {issue}")
                    hazarded = True
            except Exception as exc:  # noqa: BLE001
                hazarded = True
                diags.append(f"audit hazarded: {exc}")

            try:
                depths = count_by_class(watch_abs, glob=trigger.glob)
            except Exception as exc:  # noqa: BLE001
                hazarded = True
                diags.append(f"depth count hazarded: {exc}")
                depths = {
                    "waiting": 0,
                    "blocked": 0,
                    "capacity": 0,
                    "needs_human": 0,
                }

            summary = TriggerReconcileSummary(
                name=name,
                waiting=int(depths.get("waiting", 0)),
                blocked=int(depths.get("blocked", 0)),
                capacity=int(depths.get("capacity", 0)),
                needs_human=int(depths.get("needs_human", 0)),
                reaped=reaped_n,
                flagged_live=flagged_n,
                promoted_deferred=promoted_n,
                diagnostics=tuple(diags),
            )
            summaries.append(summary)
            print(
                f"cairn: reconcile {name}: waiting={summary.waiting} "
                f"blocked={summary.blocked} capacity={summary.capacity} "
                f"needs-human={summary.needs_human} reaped={summary.reaped} "
                f"flagged-live={summary.flagged_live} "
                f"promoted-deferred={summary.promoted_deferred}",
                file=out_s,
            )
            for line in diags:
                print(f"cairn: reconcile {name}: {line}", file=diag)

        if not summaries:
            print("cairn: reconcile: no triggers declared", file=out_s)

        return ReconcileReport(
            summaries=tuple(summaries),
            already_running=False,
            hazarded=hazarded,
        )
