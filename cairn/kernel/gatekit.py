"""Gate resolution — the human decision point, made resumable (ARCHITECTURE §3.2).

A gate's decision lands at ``runs/<id>/gates/<name>.json`` and, once written, is never
re-asked: that single file is what makes a gate resumable. Resolution order:

    already answered → skip (return the recorded choice)
    preset (``--gate name=choice``) → write ``by: "flag"``
    interactive → emit ``gate-pending``, run the TTY menu, write ``by: "tty"``
    headless with a default → write ``by: "default"``
    headless without a default → emit ``gate-pending`` and raise GateNeedsHuman (exit 6)

The gate UI is a plugin surface (ARCHITECTURE §11); this module is the built-in TTY.
It never touches the trail directly — the walker injects a locked ``emit`` callable so
gate events serialize with every other trail write. Stdlib only.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from cairn.kernel.errors import CairnError, ConfigError
from cairn.kernel.plan import GateNode
from cairn.kernel.trail import format_at

GATE_DIR = "gates"


class GateNeedsHuman(CairnError):
    """A headless run reached a gate with no default — it needs an operator (exit 6)."""


def gate_path(run_dir: Path, name: str) -> Path:
    return Path(run_dir) / GATE_DIR / f"{name}.json"


def is_answered(run_dir: Path, name: str) -> bool:
    """Whether ``gates/<name>.json`` already exists (the resume/skip predicate)."""
    return gate_path(run_dir, name).is_file()


def read_choice(run_dir: Path, name: str) -> str:
    """The recorded choice for an answered gate."""
    return json.loads(gate_path(run_dir, name).read_text(encoding="utf-8"))["choice"]


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
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))["choice"]

    options = [key for key, _ in gate.options]

    if gate.name in presets:
        choice = presets[gate.name]
        if choice not in options:
            raise ConfigError(
                f"gate {gate.name!r}: preset choice {choice!r} is not one of {options}"
            )
        return _commit(path, gate, choice, "flag", emit, now)

    if interactive:
        emit("gate-pending", node=gate.name, data={"question": gate.ask, "options": options})
        choice = _ask_tty(gate, options, prompt, out)
        return _commit(path, gate, choice, "tty", emit, now)

    if gate.default:
        return _commit(path, gate, gate.default, "default", emit, now)

    emit("gate-pending", node=gate.name, data={"question": gate.ask, "options": options})
    raise GateNeedsHuman(gate.name)


def answer_gate(run_dir: Path, name: str, choice: str) -> None:
    """Record an externally-supplied decision (the ``cairn gate`` verb's engine).

    Writes ``gates/<name>.json`` with ``by: "external"`` — the operator-pattern path: a
    headless run halts at a defaultless gate (exit 6), a human answers it out-of-band with
    ``cairn gate <run> <name>=<choice>``, and ``cairn resume`` then skips the now-answered
    gate. No trail emit here (there is no live walker); the resuming walk owns the trail.
    """
    path = gate_path(run_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "choice": choice,
        "by": "external",
        "at": format_at(datetime.now(timezone.utc)),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _commit(path: Path, gate: GateNode, choice: str, by: str, emit, now) -> str:
    """Write the decision file (atomically) then trail ``gate-answered``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"choice": choice, "by": by, "at": format_at(now)}
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
