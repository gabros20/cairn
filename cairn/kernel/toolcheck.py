"""Shared ``[tools]`` check-runner — one definition of "run a tool's ``check`` probe".

A ``[tools.X]`` entry's ``check`` is a presence+auth probe (exit 0 ⇒ the tool is installed and
authenticated on THIS machine). Two callers run it and must share exact semantics:

* ``cairn doctor`` — advisory, over every declared tool (docs/DISTRIBUTION.md §5).
* ``cairn run`` — the range-scoped hard-stop before a run mints or walks anything, for the tools
  an in-range step's ``needed_by`` names (docs/TOOLING-AND-GROWTH.md §2).

Keeping the subprocess here means the two never drift. Stdlib only.
"""

from __future__ import annotations

import os
import subprocess


def run_tool_check(check: str, *, timeout: float = 30.0) -> bool:
    """True iff ``check`` (run via ``/bin/sh -c``, inheriting the current environment) exits 0.

    A non-zero exit, a timeout, or an OS error (e.g. no shell) → False. Never raises — a broken
    or hostile ``check`` must degrade to "unverified", not crash the caller.
    """
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", check],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0
