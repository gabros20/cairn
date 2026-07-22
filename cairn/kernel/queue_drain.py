"""queue_drain — drain loop: scan → claim → run → consume.

Process orchestration for one trigger firing (docs/FACTORY-PLAN.md §3 W0.5 / D10).
Imports trigger_host (declaration) and queue_ledger (claim engine); is not imported
by either sibling.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.proc import Runner
from cairn.kernel.queue_ledger import claim, consume, scan_candidates
from cairn.kernel.trigger_host import Trigger, load_triggers, watch_dir
from cairn.kernel.types import Finding


def _run_one(
    trigger: Trigger,
    claimed_path: Path,
    workspace_dir: Path,
    runner: Runner,
    cairn_bin: str,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> bool:
    """Fire the one child ``cairn run`` for a single claimed event (TRIGGERS-PLAN.md §2
    step 3). ``claimed_path`` is ALWAYS the exact path :func:`claim` returned — including
    any ``-v2`` collision suffix — never reconstructed from the original candidate's
    basename (T1 quality finding G2 / addendum): the child must receive the ledger's
    real location, not a guess at what it might be. Already absolute (built from
    :func:`watch_dir`'s resolved watch dir), so no further resolution is applied here —
    doing so would risk dereferencing a claimed file that is itself a symlink, which T1's
    claim/consume deliberately never do.

    Mirrors ``schedkit.run_schedule``'s re-emission exactly: the Runner captures the
    child's stdout/stderr, so a firing that halts (a halt reason, a resume hint) would
    otherwise produce ZERO output and a launchd/systemd-fired trigger's operator would
    see nothing but an exit code — silently rotting, which §4 forbids. When ``out``/
    ``err`` are provided, the captured streams are re-emitted VERBATIM to them after the
    child completes; when they are None, nothing is re-emitted (matches ``run_schedule``'s
    backward-compatible default).
    """
    argv = [
        cairn_bin,
        "run",
        trigger.pipeline,
        "--headless",
        "--param",
        f"{trigger.param}={claimed_path}",
    ]
    result = runner.run(argv, cwd=workspace_dir)
    if out is not None and result.stdout:
        out.write(result.stdout)
    if err is not None and result.stderr:
        err.write(result.stderr)
    return result.returncode == 0


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
    """Drain trigger ``name``'s inbox: scan, claim each candidate, fire one ``cairn run``
    child per claim, consume on outcome (TRIGGERS-PLAN.md §2). This is the function the
    host watcher's ``cairn trigger run <name>`` argv (the T2 renderers' stable entry)
    resolves to.

    ``now`` is accepted per the module's no-hidden-clock discipline (mirroring
    schedkit's ``install``/``run_schedule`` posture) even though nothing in this
    function currently branches on it — no timestamp is embedded at this layer (the
    child's own ``run_id`` templating does that, per TRIGGERS-PLAN.md §2's closing
    paragraph). Kept as an injected parameter rather than a bare ``datetime.now()`` so a
    future addition (e.g. stuck-claim age reporting) never needs a signature change.

    ``out``/``err`` mirror ``schedkit.run_schedule`` exactly (same signature, same
    backward-compatible None default): the Runner captures each child's stdout/stderr,
    so a firing that halts would otherwise produce ZERO output on the operator's
    notification channel — the exact "silently rotting" failure run_schedule exists to
    prevent, and this is the doctrine's primary target platform (launchd/systemd-fired).
    When given, every child's captured streams are re-emitted verbatim to them via
    :func:`_run_one` as each candidate is processed. A claim/spawn/consume hazard (see
    below) has no child stream to re-emit — instead, a one-line diagnostic naming the
    candidate and the exception is written to ``err`` (falling back to ``sys.stderr``
    when ``err`` is None, so the hazard is never silent even when the caller passes
    nothing).

    Exit code: ``0`` when every candidate was processed clean, OR there was nothing to
    claim (an empty scan is a successful no-op drain, not a failure); nonzero when ANY
    candidate failed to process — its child exited nonzero, OR the claim/spawn/consume
    step itself hazarded (a raised exception, not just a nonzero child exit). A failing
    candidate never stops the drain — every remaining candidate still gets claimed and
    run (the brief's rejected alternative is retrying a failed event, not draining past
    it: draining past it is required). The failed
    claim moves to ``.failed/`` (never auto-retried) while the run overall reports
    failure via its exit code.

    :func:`claim` can raise :class:`CairnError` for a filesystem/platform hazard (a
    hardlink-unsupported platform, or ``.claim/`` on a different filesystem than the
    watch dir — T1's ``_hardlink``) instead of returning ``None``/a path. That hazard
    afflicts every candidate in this watch dir identically, not one poison file, but the
    same principle applies: a clear halt of that ONE event beats a crash of the whole
    drain. It is caught per-candidate, counted as a failure, and the loop moves on to
    the next candidate rather than aborting the whole run. Nothing was ever claimed in
    that case, so there is no claim path to :func:`consume` — the candidate is left
    exactly where it was, to be picked up again on the NEXT firing once the underlying
    filesystem/platform hazard is fixed (a structural misconfiguration, not the
    poison-file scenario ``.failed/``-and-stop targets).

    Once a candidate IS claimed, the child spawn (:func:`_run_one`) and :func:`consume`
    are wrapped in that SAME per-candidate isolation, not just ``claim()`` — a runner
    that can't even spawn the child (a missing ``cairn_bin``, a ``PermissionError``) or a
    ``consume`` that hazards while retiring the claim (its own ``_hardlink`` path can
    raise, per T1's docstring) must not abort the whole drain either
    (review-T3-quality-r1.md Finding 4). Unlike the pre-claim hazard above, this
    candidate's file IS now sitting in ``.claim/`` with no recorded outcome once such an
    exception hits — by definition a stuck claim (never auto-retried; surfaced via
    :func:`stuck_claims`/``trigger list`` for the operator to re-drop or discard by
    hand), a deliberate choice rather than a bug: a claim whose child never ran has no
    known outcome to consume it with, so surfacing it as stuck is honest, where silently
    retrying it would risk re-running a child that already did partial, unknown work.
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
            ok = _run_one(trigger, claimed, workspace_dir, runner, cairn_bin, out=out, err=err)
            consume(watch_abs, claimed, ok=ok, on_done=trigger.on_done)
        except Exception as exc:
            # The child spawn or the consume step itself hazarded (see the docstring's
            # "Once a candidate IS claimed" paragraph) — this candidate is now a stuck
            # claim by definition, not silently lost or retried; count it as a failure
            # and move on rather than aborting the whole drain (review-T3-quality-r1.md
            # Finding 4).
            any_failed = True
            print(
                f"cairn: trigger {name!r}: candidate {candidate.name!r} hazarded and was "
                f"left in .claim/ as a stuck claim: {exc}",
                file=diag,
            )
            continue
        if not ok:
            any_failed = True
    return 1 if any_failed else 0
