"""queue_drain — drain loop: sweep → scan → claim → mint → spawn → retire.

Process orchestration for one trigger firing (docs/FACTORY-PLAN.md §3 W0.5 / D10,
W1a QTP retire-side T3–T6). Imports trigger_host (declaration) and queue_ledger
(claim engine); is not imported by either sibling.

Ledger functions are looked up as **module globals** on this module (``claim``,
``retire``, ``sweep``, ``scan_candidates``). Tests patch those names here —
there is no facade setattr mirror (T5 review obligation).
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

from cairn.kernel.config import load_config
from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.plan import plan as build_plan
from cairn.kernel.proc import Runner
from cairn.kernel.queue_ledger import (
    claim,
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
from cairn.kernel.trigger_host import Trigger, load_triggers, watch_dir
from cairn.kernel.types import Finding, OutcomeClass, classify_exit


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
    return mint_new(
        workspace_dir,
        p,
        now=now,
        pipeline_hash=_pipeline_hash(workspace_dir, trigger.pipeline),
        runs_root=runs_root,
        run_dir=run_dir,
    )


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
    parks — they do NOT set failure), OR the scan was empty; nonzero when ANY
    candidate failed (FAILED outcome) or a claim/spawn/retire hazard raised.
    Guard-refusals from mint are not outcomes: item returns to the inbox, diagnostic
    only, neither failed nor parked (FACTORY-PLAN T6).

    Per-candidate isolation preserved: a hazard on one event never aborts the drain;
    post-claim exceptions leave stuck claims in ``.claim/``.
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

    for candidate in scan_candidates(watch_abs, trigger.glob):
        try:
            claimed = claim(watch_abs, candidate)
        except CairnError as exc:
            any_failed = True
            print(
                f"cairn: trigger {name!r}: candidate {candidate.name!r} hazarded and was "
                f"left in place: {exc}",
                file=diag,
            )
            continue
        if claimed is None:
            continue  # lost the claim race to a concurrent firing — not our event

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
                continue
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
                continue
            if isinstance(minted, Refusal):
                # Not an outcome: put the item back; neither failed nor parked.
                unclaim(watch_abs, claimed)
                print(
                    f"cairn: trigger {name!r}: candidate {candidate.name!r} mint refused "
                    f"({minted.kind.value}) — left in place: {minted.message}",
                    file=diag,
                )
                continue
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
            any_failed = True
            print(
                f"cairn: trigger {name!r}: candidate {candidate.name!r} hazarded and was "
                f"left in .claim/ as a stuck claim: {exc}",
                file=diag,
            )
            continue
        if outcome.outcome is OutcomeClass.FAILED:
            any_failed = True
        # WAITING parks do not set any_failed; DONE is clean.
    return 1 if any_failed else 0
