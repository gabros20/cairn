"""Gate resolution — the human decision point, made resumable (ARCHITECTURE §3.2).

A gate's decision lands at ``runs/<id>/gates/<name>.json`` and, once written by a kernel
writer, is never re-asked: that single file is what makes a gate resumable. The file is
AUTHENTICATED — it carries an HMAC (``mac``) over its ``{run,gate,choice,by,at}`` keyed by the
run's per-run secret (``gatekeys``, stored outside the agent-writable run dir). The run dir
is the agent's write scope, so a bare decision file proves nothing; only one with a valid
MAC is honored. A forged/tampered/unsigned file is treated as UNANSWERED — it emits a
``gate-tamper`` trail event and resolution falls through as if no file existed. Order:

Every OTHER reader of the decision value (the walker's ``when:`` resolver, the ``gate()``
command/prompt helper, and ``needs:`` checks) must go through ``read_verified_decision`` /
``read_verified_choice`` — same MAC + options check — so a decision forged AFTER the gate
legitimately resolved cannot flip downstream control flow or a rendered command.

    authenticated answer on disk → skip (return the recorded choice)
    preset (``--gate name=choice``) → write ``by: "flag"``
    interactive → emit ``gate-pending``, run the TTY menu, write ``by: "tty"``
    headless with a default → write ``by: "default"``
    headless without a default → emit ``gate-pending`` and raise GateNeedsHuman (exit 6)

The gate UI is a plugin surface (ARCHITECTURE §11); this module is the built-in TTY.
It never touches the trail directly — the walker injects a locked ``emit`` callable so
gate events serialize with every other trail write. Stdlib only.
"""

from __future__ import annotations

import hmac
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.gatekeys import compute_mac, ensure_run_key, load_run_key
from cairn.kernel.plan import GateNode
from cairn.kernel.trail import format_at

GATE_DIR = "gates"


class GateNeedsHuman(CairnError):
    """A headless run reached a gate with no default — it needs an operator (exit 6)."""


class GateUnanswered(CairnError):
    """A gate has no decision file yet — the normal unanswered state (not an attack)."""


class GateTampered(CairnError):
    """A gate decision file exists but failed authentication (forged/tampered/unsigned)."""


def gate_path(run_dir: Path, name: str) -> Path:
    return Path(run_dir) / GATE_DIR / f"{name}.json"


def is_answered(run_dir: Path, name: str) -> bool:
    """Whether ``gates/<name>.json`` already exists (the resume/skip predicate)."""
    return gate_path(run_dir, name).is_file()


def resolve_gate(
    gate: GateNode,
    run_dir: Path,
    *,
    interactive: bool,
    presets: dict[str, str],
    emit: Callable[..., object],
    now,
    prompt: Callable[[str], str] | None = None,
    out=None,
) -> str:
    """Resolve ``gate`` against ``run_dir``, returning the chosen option key.

    ``emit(event, node=…, data=…)`` is the walker's locked trail emitter. ``prompt`` and
    ``out`` exist for testing the TTY path; production passes neither (real ``input`` /
    ``sys.stdout``). A headless gate with a falsy ``default`` raises GateNeedsHuman.
    """
    path = gate_path(run_dir, gate.name)
    options = [key for key, _ in gate.options]

    if path.is_file():
        trusted = _verify_decision(run_dir, gate, path, options, emit)
        if trusted is not None:
            return trusted["choice"]
        # Forged/tampered/unauthenticated: `_verify_decision` emitted `gate-tamper`. Fall
        # through to the normal unanswered path — an operator preset, an interactive re-ask,
        # a headless default, or GateNeedsHuman. The forged file is NEVER honored.

    if gate.name in presets:
        choice = presets[gate.name]
        if choice not in options:
            raise ConfigError(
                f"gate {gate.name!r}: preset choice {choice!r} is not one of {options}"
            )
        return _commit(run_dir, path, gate, choice, "flag", emit, now)

    if interactive:
        emit("gate-pending", node=gate.name, data={"question": gate.ask, "options": options})
        choice = _ask_tty(gate, options, prompt, out)
        return _commit(run_dir, path, gate, choice, "tty", emit, now)

    if gate.default:
        return _commit(run_dir, path, gate, gate.default, "default", emit, now)

    emit("gate-pending", node=gate.name, data={"question": gate.ask, "options": options})
    raise GateNeedsHuman(gate.name)


def answer_gate(run_dir: Path, name: str, choice: str) -> None:
    """Record an externally-supplied decision (the ``cairn gate`` verb's engine).

    Writes ``gates/<name>.json`` with ``by: "external"`` — the operator-pattern path: a
    headless run halts at a defaultless gate (exit 6), a human answers it out-of-band with
    ``cairn gate <run> <name>=<choice>``, and ``cairn resume`` then skips the now-answered
    gate. No trail emit here (there is no live walker); the resuming walk owns the trail.
    """
    run_dir = Path(run_dir)
    path = gate_path(run_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    at = format_at(datetime.now(timezone.utc))
    secret = ensure_run_key(run_dir)
    payload = {
        "choice": choice,
        "by": "external",
        "at": at,
        "mac": compute_mac(secret, run_dir, name, choice, "external", at),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _verify_decision(
    run_dir: Path, gate: GateNode, path: Path, options: list[str], emit
) -> dict | None:
    """Return the authenticated decision dict for an existing file, else None.

    A file is trusted only if it carries a ``mac`` that HMAC-verifies (with the run's secret)
    over its own ``{run,gate,choice,by,at}`` AND its ``choice`` is one of the gate's options. On
    ANY failure — unreadable file, missing/blank ``mac``, missing/unreadable secret, MAC
    mismatch, or off-menu choice — it emits a ``gate-tamper`` trail event and returns None so
    the caller re-asks/halts. Fails SAFE: a missing secret is tamper, never "trust the file".
    """

    def tamper(reason: str) -> None:
        emit("gate-tamper", node=gate.name, data={"gate": gate.name, "reason": reason})

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tamper("unreadable")
        return None
    if not isinstance(doc, dict):
        tamper("malformed")
        return None

    mac = doc.get("mac")
    if not isinstance(mac, str) or not mac:
        tamper("missing-mac")
        return None

    secret = load_run_key(run_dir)
    if secret is None:
        tamper("missing-secret")
        return None

    choice, by, at = doc.get("choice"), doc.get("by"), doc.get("at")
    expected = compute_mac(secret, run_dir, gate.name, choice, by, at)
    if not hmac.compare_digest(expected, mac):
        tamper("mac-mismatch")
        return None

    if choice not in options:
        tamper("choice-not-an-option")
        return None

    return doc


def _noop_emit(*_a, **_k) -> None:
    return None


def read_verified_decision(run_dir: Path, gate: GateNode, emit=_noop_emit) -> dict:
    """The authenticated decision dict for an already-answered gate — for readers OTHER than
    ``resolve_gate`` (the ``when:`` resolver, the ``gate()`` command helper, ``needs``).

    Raises :class:`GateUnanswered` if no decision file exists, and :class:`GateTampered` (after
    emitting ``gate-tamper``) if one exists but fails MAC/options verification. A tampered file is
    thus treated no better than a missing one for these consumers — closing the post-resolution
    forge window where a compromised step overwrites a legitimately-signed decision within one
    walk. ``emit`` defaults to a no-op for callers with no live trail (the ``cairn gate`` CLI).
    """
    run_dir = Path(run_dir)
    path = gate_path(run_dir, gate.name)
    if not path.is_file():
        raise GateUnanswered(gate.name)
    options = [key for key, _ in gate.options]
    doc = _verify_decision(run_dir, gate, path, options, emit)
    if doc is None:
        raise GateTampered(gate.name)
    return doc


def read_verified_choice(run_dir: Path, gate: GateNode, emit=_noop_emit) -> str:
    """The authenticated ``choice`` for an already-answered gate (see read_verified_decision)."""
    return read_verified_decision(run_dir, gate, emit)["choice"]


def _commit(run_dir: Path, path: Path, gate: GateNode, choice: str, by: str, emit, now) -> str:
    """Write the (signed) decision file atomically then trail ``gate-answered``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    at = format_at(now)
    secret = ensure_run_key(run_dir)
    payload = {
        "choice": choice, "by": by, "at": at,
        "mac": compute_mac(secret, run_dir, gate.name, choice, by, at),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    emit("gate-answered", node=gate.name, data={"choice": choice, "by": by})
    return choice


def _ask_tty(gate: GateNode, options: list[str], prompt, out) -> str:
    """Render the menu and read a valid choice, re-asking until one is given."""
    stream = out if out is not None else sys.stdout
    ask = prompt if prompt is not None else input
    print(gate.ask, file=stream)
    for key, desc in gate.options:
        print(f"  {key}: {desc}", file=stream)
    if gate.reads:
        print(f"(reads: {', '.join(gate.reads)})", file=stream)
    while True:
        try:
            answer = ask(f"choice [{'/'.join(options)}]: ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            # A closed/interrupted TTY is the operator declining to answer, not a crash:
            # surface it as the typed needs-human signal the walker turns into exit 6.
            raise GateNeedsHuman(gate.name) from exc
        if answer in options:
            return answer
        print(f"{answer!r} is not one of {options}", file=stream)
