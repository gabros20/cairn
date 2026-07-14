"""The guards engine — pre-execution command policy in three layers (ARCHITECTURE §4).

A guard is a declared policy on commands (``GuardDecl``): a ``match`` (tool + glob command
pattern), a ``check`` script (exit 0 allow / exit 2 deny + stderr reason — API §4), an
``on_error`` disposition (fail-open ``allow`` / fail-closed ``deny``), and the ``enforce``
layers it wants. This module is the machinery behind three of the four layers; the walker
wires them to executors.

Layers (ARCHITECTURE §4 matrix):

* ``hook`` — the executor's native pre-tool hook (Claude deny-JSON, Grok exit-2, Codex if it
  fires). Executor-specific; not built here.
* ``shim`` — a PATH wrapper this module installs (``build_shims``) that works on *any* executor,
  even a bare shell. It reconstructs the command line, runs the guard chain via
  ``python -m cairn.kernel.guards --shim-check``, and — on allow — execs the real binary found on
  PATH *minus* the shim dir, passing its exit code straight through.
* ``post`` — needs no code here: it is the artifact validator, already the walker's hard gate
  after every step. A command that slipped past hook+shim still cannot poison a downstream step,
  because the validator rejects the bad artifact before the next step consumes it.

The check contract (``run_check``): the check is handed ``{"command", "env", "run_dir"}`` on stdin
as JSON. ``env`` is a SAFE SUBSET — only ``CAIRN_*`` keys — so a check can never be used to
exfiltrate a secret the step happened to hold. Exit 0 → allowed; exit 2 → denied (last stderr line
is the reason); ANY other outcome (other exit code, timeout, spawn failure, crash) is resolved by
``on_error``: fail-open with a warning, or fail-closed naming the error. No shell is ever invoked.

Two boundary facts:

* The shim layer intercepts a *binary name* resolved via PATH; invoking a guarded binary by
  ABSOLUTE PATH sidesteps the shim. That is by design — the shim is one of three defence layers,
  and the executor's ``hook`` and the ``post`` validator still cover an absolute-path invocation
  (ARCHITECTURE §4). A guard must never rely on the shim alone.
* Guard *runner* infrastructure failure (the ``--shim-check`` interpreter itself failing to
  start, distinct from a check script erroring) fails CLOSED: the shim propagates the non-zero
  code and the real binary is not run. A deny can never silently flip to allow; the trade is
  availability, taken deliberately.

Stdlib only.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cairn.kernel.errors import ConfigError
from cairn.kernel.plan import GuardDecl, _binary_name

_SHIM_MARKER = "# cairn guard shim for"


@dataclass(frozen=True)
class CheckResult:
    """The verdict of one guard check on one command."""

    allowed: bool
    reason: str | None = None


def matches(guard: GuardDecl, *, tool: str, command: str) -> bool:
    """Does ``guard`` apply to this ``(tool, command)``?

    ``match_tool`` is an EXACT match ("bash" etc.); an empty ``match_tool`` imposes no tool
    constraint. ``match_command`` is a glob (``fnmatch``, case-sensitive) over the WHOLE command
    string, where ``*`` crosses spaces — so a two-token pattern spans multiple args. Example:
    ``"brease* createMedia*"`` matches ``"brease media createMedia --file x.png"``. An empty
    ``match_command`` imposes no command constraint.
    """
    if guard.match_tool and guard.match_tool != tool:
        return False
    if not guard.match_command:
        return True
    return fnmatch.fnmatchcase(command, guard.match_command)


def _check_argv(check: Path) -> list[str]:
    """Spawn a ``.py`` check with the current interpreter; anything else directly."""
    if check.suffix == ".py":
        return [sys.executable, str(check)]
    return [str(check)]


def _safe_env(env: dict[str, str]) -> dict[str, str]:
    """The only env a check ever sees: ``CAIRN_*`` keys. Secrets never cross this line."""
    return {k: v for k, v in env.items() if k.startswith("CAIRN_")}


# The minimal system keys a check process needs to run (so a .py check's interpreter can
# start and use a temp dir) — pulled from the runner's own environment, never the step's.
# Everything else in os.environ (secrets, tokens) is withheld from the check process.
_SYSTEM_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
)


def _check_process_env(env: dict[str, str]) -> dict[str, str]:
    """The environment the check SUBPROCESS runs with: the ``CAIRN_*`` subset of the step's
    ``env`` plus a minimal system allowlist for the interpreter — and nothing else. This is the
    real containment boundary: filtering only the stdin payload would still leak every secret in
    the ambient process env through inheritance."""
    proc_env = _safe_env(env)
    for key in _SYSTEM_ENV_KEYS:
        val = os.environ.get(key)
        if val is not None:
            proc_env.setdefault(key, val)
    return proc_env


def _last_line(text: str, default: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else default


def run_check(
    guard: GuardDecl,
    *,
    command: str,
    env: dict[str, str],
    run_dir: Path,
    workspace_dir: Path,
    timeout_s: int = 30,
) -> CheckResult:
    """Run ``guard``'s check against ``command`` and return the verdict.

    stdin is ``{"command", "env" (CAIRN_* only), "run_dir"}``; cwd is the workspace so a check
    can import siblings, while the run dir it should inspect is passed explicitly. Exit 0 allows,
    exit 2 denies (reason = last stderr line); any other outcome is resolved by ``on_error``.
    """
    payload = json.dumps(
        {"command": command, "env": _safe_env(env), "run_dir": str(run_dir)}
    )
    cwd = str(workspace_dir) if workspace_dir and Path(workspace_dir).is_dir() else None
    try:
        proc = subprocess.run(
            _check_argv(guard.check),
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=cwd,
            env=_check_process_env(env),
        )
    except subprocess.TimeoutExpired:
        return _resolve_error(guard, f"timed out after {timeout_s}s")
    except OSError as exc:
        return _resolve_error(guard, f"could not spawn ({exc})")
    if proc.returncode == 0:
        return CheckResult(allowed=True, reason=None)
    if proc.returncode == 2:
        return CheckResult(allowed=False, reason=_last_line(proc.stderr, "denied"))
    detail = _last_line(proc.stderr, f"exit {proc.returncode}")
    return _resolve_error(guard, f"exited {proc.returncode} ({detail})")


def _resolve_error(guard: GuardDecl, what: str) -> CheckResult:
    """Apply ``on_error`` to an ERROR outcome (not a clean 0/2)."""
    if guard.on_error == "allow":
        return CheckResult(
            allowed=True, reason=f"guard {guard.name!r} check {what}; failing open"
        )
    return CheckResult(
        allowed=False, reason=f"guard {guard.name!r} check {what}; failing closed"
    )


# --------------------------------------------------------------------------- #
# The shim layer — PATH wrappers that work on any executor (ARCHITECTURE §4).
# --------------------------------------------------------------------------- #

# _binary_name lives in plan.py (this module already imports GuardDecl from there, and
# plan.py's guard-parse pass shares this exact derivation for its plan-time basename check
# — codex-F4).


# One /bin/sh shim per guarded binary. It reconstructs the command line, defers the
# allow/deny decision to the guard chain (this module's --shim-check entry), and — on
# allow — execs the REAL binary found on PATH minus the shim dir, so the real exit code
# passes straight through. Placeholders are substituted at build time.
_SHIM_TEMPLATE = """\
#!/bin/sh
# cairn guard shim for {binary!r} — regenerated by build_shims(); do not edit.
SHIM_DIR='{shim_dir}'
CAIRN_SHIM_DIR="$SHIM_DIR"; export CAIRN_SHIM_DIR
CAIRN_SHIM_MANIFEST="$SHIM_DIR/manifest.json"; export CAIRN_SHIM_MANIFEST
bin='{binary}'
cmd="$bin"
for a in "$@"; do
  cmd="$cmd $a"
done
CAIRN_SHIM_COMMAND="$cmd"; export CAIRN_SHIM_COMMAND
# re-entrancy marker: if a shim for this binary is already on the stack we must not run the
# guard chain again (a symlinked/aliased shim dir on PATH could otherwise recurse into us).
case ":$CAIRN_SHIM_ACTIVE:" in
  *":$bin:"*) ;;  # already inside a shim for $bin → skip straight to the real binary
  *)
    CAIRN_SHIM_ACTIVE="$CAIRN_SHIM_ACTIVE:$bin"; export CAIRN_SHIM_ACTIVE
    '{python}' -m cairn.kernel.guards --shim-check {names} || exit $?
    ;;
esac
# allowed → locate and exec the real binary (PATH minus the shim dir). We exclude by exact
# string first, then by PHYSICAL path (symlinks + trailing slashes resolved) so a second,
# differently-spelled reference to the shim dir on PATH cannot re-select this shim.
real=''
SHIM_PHYS=$(cd "$SHIM_DIR" 2>/dev/null && pwd -P)
oldifs="$IFS"
IFS=':'
for d in $PATH; do
  [ -z "$d" ] && continue
  [ "$d" = "$SHIM_DIR" ] && continue
  dphys=$(cd "$d" 2>/dev/null && pwd -P)
  [ -n "$dphys" ] && [ "$dphys" = "$SHIM_PHYS" ] && continue
  if [ -x "$d/$bin" ]; then real="$d/$bin"; break; fi
done
IFS="$oldifs"
if [ -z "$real" ]; then
  echo "cairn shim: real '$bin' not found on PATH (excluding shim dir)" >&2
  exit 127
fi
exec "$real" "$@"
"""


def _guard_json(guard: GuardDecl) -> dict[str, object]:
    return {
        "match_tool": guard.match_tool,
        "match_command": guard.match_command,
        "check": str(guard.check),
        "enforce": list(guard.enforce),
        "on_error": guard.on_error,
    }


def build_shims(
    guards: Sequence[GuardDecl], *, shim_dir: Path, workspace_dir: Path
) -> dict[str, str]:
    """Install a PATH-shim per guarded binary into ``shim_dir`` and return the env delta.

    Every guard in ``guards`` is shimmed (the caller passes the ``shim``-enforced subset).
    Guards sharing a binary collapse into one shim that runs them as a chain (first deny wins).
    A compact ``manifest.json`` beside the shims records the guard decls so the ``--shim-check``
    entry can re-load them. Rebuilding is idempotent (byte-identical output for equal input).

    Returns ``{"PATH": <shim_dir>, "CAIRN_SHIM_DIR": <shim_dir>}`` — the caller PREPENDS ``PATH``
    to the executor step's existing PATH (``f"{delta['PATH']}:{existing}"``); ``CAIRN_SHIM_DIR``
    lets the shim exclude itself when finding the real binary. Empty ``guards`` → ``{}`` (no-op).
    """
    if not guards:
        return {}

    shim_dir = Path(shim_dir)
    shim_dir.mkdir(parents=True, exist_ok=True)

    # Preserve declaration order per binary so the chain is deterministic.
    by_binary: dict[str, list[GuardDecl]] = {}
    for g in guards:
        binary = _binary_name(g.match_command)
        if not binary:
            # A shim needs a concrete binary to wrap; a leading-glob (or empty) match_command
            # has no prefix to derive one from, so it cannot be enforced at the shim layer.
            raise ConfigError(
                f"guard {g.name!r}: match_command {g.match_command!r} has no concrete binary "
                "prefix to build a shim for (remove the 'shim' layer or anchor the pattern)"
            )
        by_binary.setdefault(binary, []).append(g)

    # Sweep stale cairn shims left from a previous build into a reused dir: a shim for a binary
    # no longer guarded would find no matching guard and silently wave the command through.
    for entry in shim_dir.iterdir():
        if entry.name == "manifest.json" or entry.name in by_binary or not entry.is_file():
            continue
        try:
            head = entry.read_text(encoding="utf-8", errors="ignore")[:200]
        except OSError:
            continue
        if _SHIM_MARKER in head:  # only sweep files we generated; leave foreign files alone
            entry.unlink()

    manifest = {
        "workspace_dir": str(workspace_dir),
        "guards": {g.name: _guard_json(g) for g in guards},
    }
    (shim_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    for binary, chain in by_binary.items():
        names = " ".join(f"'{g.name}'" for g in chain)
        body = _SHIM_TEMPLATE.format(
            binary=binary, shim_dir=shim_dir, python=sys.executable, names=names
        )
        shim = shim_dir / binary
        # Defense-in-depth backstop (codex-F4): plan.py's guard-parse pass already rejects a
        # slashed/traversing binary before any plan reaches here, but a caller that builds
        # GuardDecls directly (bypassing plan validation) must not be able to smuggle one
        # through — a binary containing '/' collapses `shim_dir / binary` to an absolute path
        # (Path("/a") / "/b" == Path("/b")), landing write_text+chmod OUTSIDE the shim dir.
        if shim.resolve().parent != shim_dir.resolve():
            raise ConfigError(
                f"guard binary {binary!r} resolves outside the shim dir {shim_dir} — "
                "refusing to write it (a bare command name is required)"
            )
        shim.write_text(body, encoding="utf-8")
        shim.chmod(0o755)

    return {"PATH": str(shim_dir), "CAIRN_SHIM_DIR": str(shim_dir)}


# --------------------------------------------------------------------------- #
# The --shim-check entry: what a shim invokes to run its guard chain.
# --------------------------------------------------------------------------- #


def _load_manifest_guard(name: str, raw: dict[str, object]) -> GuardDecl:
    return GuardDecl(
        name=name,
        match_tool=str(raw.get("match_tool", "")),
        match_command=str(raw.get("match_command", "")),
        check=Path(str(raw["check"])),
        enforce=tuple(raw.get("enforce", []) or []),  # type: ignore[arg-type]
        on_error=str(raw.get("on_error", "deny")),
        when=None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """The ``python -m cairn.kernel.guards --shim-check NAME...`` entry a shim calls.

    Reads the invoked command / run dir / manifest from the ``CAIRN_*`` env the shim exports,
    re-loads the named guards from the manifest, and runs the chain (matches → run_check). Exit
    0 → allow (the shim then execs the real binary); exit 2 → deny with the reason on stderr.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "--shim-check":
        print("usage: python -m cairn.kernel.guards --shim-check NAME...", file=sys.stderr)
        return 2  # fail-closed: a malformed invocation must not wave a command through
    names = args[1:]

    manifest_path = os.environ.get("CAIRN_SHIM_MANIFEST")
    if not manifest_path:
        shim_dir = os.environ.get("CAIRN_SHIM_DIR")
        manifest_path = str(Path(shim_dir) / "manifest.json") if shim_dir else None
    if not manifest_path or not Path(manifest_path).is_file():
        print("cairn guard: shim manifest not found", file=sys.stderr)
        return 2
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    guards_raw = manifest.get("guards", {})
    workspace_dir = Path(manifest.get("workspace_dir", os.getcwd()))

    command = os.environ.get("CAIRN_SHIM_COMMAND", "")
    tool = os.environ.get("CAIRN_SHIM_TOOL", "bash")
    run_dir = Path(os.environ.get("CAIRN_RUN_DIR", os.getcwd()))

    for name in names:
        raw = guards_raw.get(name)
        if raw is None:
            continue
        g = _load_manifest_guard(name, raw)
        if not matches(g, tool=tool, command=command):
            continue
        result = run_check(
            g,
            command=command,
            env=dict(os.environ),
            run_dir=run_dir,
            workspace_dir=workspace_dir,
        )
        if not result.allowed:
            print(result.reason or "denied", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
