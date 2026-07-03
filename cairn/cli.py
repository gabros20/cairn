"""cairn CLI entry point — the argparse frame only.

Every subcommand is registered so `cairn <cmd> --help` and shell completion see the
real surface, but each is a stub that reports "not implemented yet". Real wiring
(plan/run/resume/…) lands in later tasks; this file stays deliberately tiny.
"""

from __future__ import annotations

import argparse
import sys

import cairn
from cairn.kernel.types import ExitCode

# The full verb surface (docs/API.md §9). Order is the help-listing order.
SUBCOMMANDS: list[str] = [
    "plan",
    "run",
    "resume",
    "gate",
    "validate",
    "trail",
    "ps",
    "doctor",
    "test",
    "new",
    "compose",
    "batch",
    "learnings",
    "gc",
    "schedule",
]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cairn", description="cairn pipeline orchestrator")
    parser.add_argument(
        "--version",
        action="version",
        version=f"cairn {cairn.__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for name in SUBCOMMANDS:
        subparsers.add_parser(name, help=f"{name} (not implemented yet)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return int(ExitCode.CONFIG)
    print(f"not implemented yet: {args.command}", file=sys.stderr)
    return int(ExitCode.CONFIG)
