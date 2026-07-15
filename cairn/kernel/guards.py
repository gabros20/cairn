"""The guards engine — pre-execution command policy in three layers (ARCHITECTURE §4).

A guard is a declared policy on commands (``GuardDecl``): a ``match`` (tool + glob command
pattern), a ``check`` script (exit 0 allow / exit 2 deny + stderr reason — API §4), an
``on_error`` disposition (fail-open ``allow`` / fail-closed ``deny``), and the ``enforce``
layers it wants. This module is the machinery behind three of the four layers; the walker
wires them to executors.

Layers (ARCHITECTURE §4 matrix):

* ``hook`` — the executor's native pre-tool hook (Claude deny-JSON, Grok exit-2, Codex if it
  fires). The install is executor-specific (``ClaudeExecutor.install_guards`` writes the
  ``PreToolUse`` settings), but the DECISION runs here: the ``--hook-check`` entry reads the
  PreToolUse event from stdin and runs the SAME guard chain as ``--shim-check`` (``_run_chain``),
  blocking via deny-JSON on stdout and failing CLOSED on any error.
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
  ABSOLUTE PATH sidesteps the shim. The ``hook`` does NOT cover this gap: it matches the same
  ``fnmatch`` glob over the command string, so ``/usr/local/bin/brease …``, ``sh -c "brease …"``,
  ``env X=1 brease …`` and a leading-space command all MISS a ``brease*`` guard exactly as the
  shim does (H1). The only layer that catches those is ``post`` — the artifact validator, which
  inspects results, not command strings. A guard must never rely on the shim OR the hook alone.
* Guard *runner* infrastructure failure (the ``--shim-check`` interpreter itself failing to
  start, distinct from a check script erroring) fails CLOSED: the shim propagates the non-zero
  code and the real binary is not run. A deny can never silently flip to allow; the trade is
  availability, taken deliberately.

Stdlib only.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cairn.kernel.errors import ConfigError
from cairn.kernel.gatekeys import (
    compute_content_mac,
    ensure_run_key,
    guard_manifest_path,
    load_run_key,
)
from cairn.kernel.plan import GuardDecl, _binary_name

_SHIM_MARKER = "# cairn guard shim for"


@dataclass(frozen=True)
class CheckResult:
    """The verdict of one guard check on one command.

    ``failed_open`` is True only for the ``on_error == "allow"`` branch of ``_resolve_error``
    — an ALLOW that happened because the check crashed/timed out/errored, not because it ran
    and said yes (codex-F18). It never affects the allow/deny decision itself; it exists so a
    consumer can surface the degraded-check case (W6-B) without string-matching ``reason``.
    """

    allowed: bool
    reason: str | None = None
    failed_open: bool = False


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
            allowed=True,
            reason=f"guard {guard.name!r} check {what}; failing open",
            failed_open=True,
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
# The manifest lives OUTSIDE the run dir (gatekeys-protected, agent-unwritable) and is baked
# absolute here so it resolves at shim-fire time; --shim-check authenticates it (per-run MAC).
CAIRN_SHIM_MANIFEST='{manifest}'; export CAIRN_SHIM_MANIFEST
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


def _sha256_file(path: Path) -> str | None:
    """The sha256 hex of ``path``'s bytes, or None if unreadable. Used to PIN each guard's check
    script: the hash is signed into the manifest so a swapped check (rewritten to ``exit 0``) is
    detected at decision time even where the agent can write the script (C2)."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _guard_json(guard: GuardDecl) -> dict[str, object]:
    return {
        "match_tool": guard.match_tool,
        "match_command": guard.match_command,
        "check": str(guard.check),
        "check_sha256": _sha256_file(guard.check),
        "enforce": list(guard.enforce),
        "on_error": guard.on_error,
    }


def build_manifest(
    guards: Sequence[GuardDecl], *, workspace_dir: Path, run_dir: Path
) -> dict[str, object]:
    """The compact ``{workspace_dir, guards:{name:decl}, mac}`` a guard-check entry re-loads and
    AUTHENTICATES.

    ONE shape shared by the shim manifest (``build_shims``) and the hook manifest
    (``ClaudeExecutor.install_guards``) so ``--shim-check`` and ``--hook-check`` read the same
    thing — a drift between the two would be a security bug. Each guard decl carries a
    ``check_sha256`` pinning its check script. The whole content is signed with the per-run secret
    (``gatekeys``, held OUTSIDE the run dir) so a manifest the agent rewrites — to drop guards, to
    flip a decl, or to point at a tampered check — fails verification at decision time (C1/C2).
    ``ensure_run_key`` reuses the secret already minted at ``bootstrap_run``."""
    content: dict[str, object] = {
        "workspace_dir": str(workspace_dir),
        "guards": {g.name: _guard_json(g) for g in guards},
    }
    secret = ensure_run_key(Path(run_dir))
    return {**content, "mac": compute_content_mac(secret, Path(run_dir), content)}


def _write_manifest_file(path: Path, content: dict) -> None:
    """Write a signed manifest atomically (dir 0700, file 0600 — mirrors the gate-key hygiene) to
    ``path``, a protected location OUTSIDE the agent's run dir."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass  # best-effort; a pre-existing looser dir is the operator's call (mirrors gatekeys)
    body = json.dumps(content, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def write_manifest(
    guards: Sequence[GuardDecl], *, workspace_dir: Path, run_dir: Path, path: Path
) -> None:
    """Build the signed manifest for ``guards`` and write it atomically to ``path`` (a protected,
    agent-unwritable location — see ``gatekeys.guard_manifest_path``). Shared by ``build_shims``
    and ``ClaudeExecutor.install_guards`` so the shim and hook manifests are produced,
    authenticated, and persisted IDENTICALLY."""
    _write_manifest_file(
        path, build_manifest(guards, workspace_dir=workspace_dir, run_dir=run_dir)
    )


def _load_verified_manifest(
    manifest_path: str | None, run_dir: Path
) -> tuple[dict, Path] | None:
    """Load AND authenticate a guard manifest. Returns ``(guards_raw, workspace_dir)`` on success,
    or ``None`` on ANY failure — the caller then FAILS CLOSED.

    Fails closed on: no path, missing/unreadable file, non-JSON, non-dict top level, missing/empty
    MAC, missing per-run secret, MAC mismatch (constant-time), or a malformed shape (``guards`` not
    a dict / ``workspace_dir`` not a string). The MAC proves the runner wrote it, not the
    sandboxed agent (C1); the per-guard ``check_sha256`` is re-verified in ``_run_chain`` (C2)."""
    if not manifest_path:
        return None
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    mac = manifest.get("mac")
    if not isinstance(mac, str) or not mac:
        return None
    secret = load_run_key(Path(run_dir))  # NEVER mints — a missing secret is tamper → fail closed
    if secret is None:
        return None
    content = {k: v for k, v in manifest.items() if k != "mac"}
    expected = compute_content_mac(secret, Path(run_dir), content)
    if not hmac.compare_digest(expected, mac):
        return None
    guards_raw = manifest.get("guards")
    workspace_raw = manifest.get("workspace_dir")
    if not isinstance(guards_raw, dict) or not isinstance(workspace_raw, str):
        return None
    return guards_raw, Path(workspace_raw)


def deny_json(reason: str) -> str:
    """The claude/codex ``PreToolUse`` deny payload (printed to STDOUT to BLOCK a tool).

    claude/codex honor ``hookSpecificOutput.permissionDecision == "deny"`` for ``PreToolUse``
    (verified against claude 2.1.199 / codex-cli 0.142.5 — see hookprobe). The single builder
    both the production ``--hook-check`` entry and the ``hookprobe`` canary use, so the two can
    never drift. ``reason`` becomes ``permissionDecisionReason`` (must be non-empty)."""
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def build_shims(
    guards: Sequence[GuardDecl], *, shim_dir: Path, workspace_dir: Path, run_dir: Path
) -> dict[str, str]:
    """Install a PATH-shim per guarded binary into ``shim_dir`` and return the env delta.

    Every guard in ``guards`` is shimmed (the caller passes the ``shim``-enforced subset).
    Guards sharing a binary collapse into one shim that runs them as a chain (first deny wins).
    The guard decls are recorded in a SIGNED manifest written OUTSIDE the run dir
    (``gatekeys.guard_manifest_path(run_dir, "shim")``) so the sandboxed agent cannot rewrite the
    policy its own commands are checked against; the absolute manifest path is baked into each
    shim and ``--shim-check`` authenticates it. Rebuilding is idempotent (byte-identical output).

    Returns ``{"PATH", "CAIRN_SHIM_DIR", "CAIRN_SHIM_MANIFEST"}`` — the caller PREPENDS ``PATH``
    to the executor step's existing PATH (``f"{delta['PATH']}:{existing}"``); ``CAIRN_SHIM_DIR``
    lets the shim exclude itself when finding the real binary; ``CAIRN_SHIM_MANIFEST`` is the
    protected manifest path. Empty ``guards`` → ``{}`` (no-op).
    """
    if not guards:
        return {}

    shim_dir = Path(shim_dir)
    shim_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = guard_manifest_path(Path(run_dir), "shim")

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
    # no longer guarded would find no matching guard and silently wave the command through. (The
    # manifest no longer lives here — it is in the protected dir — so only shim scripts remain.)
    for entry in shim_dir.iterdir():
        if entry.name in by_binary or not entry.is_file():
            continue
        try:
            head = entry.read_text(encoding="utf-8", errors="ignore")[:200]
        except OSError:
            continue
        if _SHIM_MARKER in head:  # only sweep files we generated; leave foreign files alone
            entry.unlink()

    write_manifest(guards, workspace_dir=workspace_dir, run_dir=run_dir, path=manifest_path)

    for binary, chain in by_binary.items():
        names = " ".join(f"'{g.name}'" for g in chain)
        body = _SHIM_TEMPLATE.format(
            binary=binary, shim_dir=shim_dir, python=sys.executable, names=names,
            manifest=manifest_path,
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

    return {
        "PATH": str(shim_dir),
        "CAIRN_SHIM_DIR": str(shim_dir),
        "CAIRN_SHIM_MANIFEST": str(manifest_path),
    }


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


def _run_chain(
    names: Sequence[str],
    guards_raw: dict,
    *,
    tool: str,
    command: str,
    workspace_dir: Path,
    run_dir: Path,
    env: dict[str, str],
) -> CheckResult:
    """Run the guard chain for ``names`` against ``(tool, command)`` — the ONE enforcement loop
    both ``--shim-check`` and ``--hook-check`` call, so the two layers can never diverge (a
    divergence between shim and hook enforcement is a security bug). Each named guard is loaded
    from the manifest, ``matches``-filtered, then ``run_check``-ed; the FIRST deny wins and is
    returned. No matching deny → an allow. Callers own how a deny is COMMUNICATED (shim: exit 2 +
    stderr; hook: deny-JSON on stdout)."""
    for name in names:
        raw = guards_raw.get(name)
        if raw is None:
            continue
        g = _load_manifest_guard(name, raw)
        if not matches(g, tool=tool, command=command):
            continue
        # Integrity-pin the check script (C2): re-hash it and compare to the value SIGNED into the
        # manifest. A check the agent rewrote to `exit 0` no longer matches the signed hash → deny.
        # Fail closed on any ambiguity (unreadable check → None; missing/again-non-string signed
        # hash). The manifest MAC is verified by the caller before we get here.
        signed = raw.get("check_sha256") if isinstance(raw, dict) else None
        current = _sha256_file(g.check)
        if current is None or not isinstance(signed, str) or current != signed:
            return CheckResult(
                allowed=False,
                reason=f"cairn guard: check script for {name!r} failed integrity check "
                "(tampered or missing)",
            )
        result = run_check(
            g,
            command=command,
            env=env,
            run_dir=run_dir,
            workspace_dir=workspace_dir,
        )
        if result.failed_open:
            # Purely additive (W6-B, codex-F18): the check errored and this guard fails OPEN,
            # so the command runs with no diagnostic anywhere unless we surface it here.
            # Never changes the outcome — result.allowed is already True in this branch;
            # this is only visibility for a broken guard that waved a command through.
            print(result.reason or f"guard {g.name!r}: failed open", file=sys.stderr)
        if not result.allowed:
            return result
    return CheckResult(allowed=True, reason=None)


def _shim_check(names: Sequence[str]) -> int:
    """The ``--shim-check`` body: reads the command / run dir / manifest from the ``CAIRN_*`` env
    the shim exports, AUTHENTICATES the manifest, runs the chain, and returns exit 0 (allow) / 2
    (deny, reason on stderr). Any manifest that fails verification denies (exit 2) — fail closed."""
    run_dir = Path(os.environ.get("CAIRN_RUN_DIR", os.getcwd()))
    manifest_path = os.environ.get("CAIRN_SHIM_MANIFEST")
    if not manifest_path:
        manifest_path = str(guard_manifest_path(run_dir, "shim"))
    loaded = _load_verified_manifest(manifest_path, run_dir)
    if loaded is None:
        print("cairn guard: shim manifest missing or failed verification", file=sys.stderr)
        return 2
    guards_raw, workspace_dir = loaded

    command = os.environ.get("CAIRN_SHIM_COMMAND", "")
    tool = os.environ.get("CAIRN_SHIM_TOOL", "bash")

    result = _run_chain(
        names, guards_raw, tool=tool, command=command,
        workspace_dir=workspace_dir, run_dir=run_dir, env=dict(os.environ),
    )
    if not result.allowed:
        print(result.reason or "denied", file=sys.stderr)
        return 2
    return 0


# Map a claude/codex PreToolUse ``tool_name`` to a cairn guard tool. cairn's guards are command
# guards; ``Bash`` → ``bash`` is the real case (the shim layer covers the same). Any other tool
# lowercases through — a guard with a matching ``match_tool`` still applies, and a Bash-only guard
# simply won't ``match``.
def _hook_command(event: dict) -> tuple[str, str | None]:
    """From a PreToolUse event ``{"tool_name", "tool_input"}`` derive ``(tool, command)``. The
    command is ``tool_input.command`` (Bash and other command-carrying tools). A tool with no
    command string yields ``None`` — no command-glob guard can apply, so the caller allows."""
    tool = str(event.get("tool_name", "")).lower()
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    return tool, command if isinstance(command, str) and command else None


def _hook_fail_closed(reason: str) -> int:
    """A claude ``PreToolUse`` hook that cannot reach a decision must BLOCK, never wave the tool
    through. Emit the deny-JSON to stdout and exit **0** — the deny-JSON on stdout is the
    PROVEN-blocking form (hookprobe.ClaudeHookRecipe exits 0 and blocks via stdout); a non-zero
    exit is NOT proven to win over the JSON and risks a "hook errored → allow" fail-OPEN (M2). The
    verdict is carried entirely by the JSON, exactly like the clean-deny path."""
    sys.stdout.write(deny_json(reason))  # no trailing newline — mirrors the probe-verified form
    return 0


def _hook_check(names: Sequence[str]) -> int:
    """The ``--hook-check`` body a claude ``PreToolUse`` hook invokes. Reads the event JSON from
    STDIN, AUTHENTICATES the guard manifest, runs the SAME guard chain as ``--shim-check``
    (``_run_chain``), and blocks by printing the deny-JSON to STDOUT. ALLOW → no output, exit 0.
    Any error (unreadable stdin, missing/tampered/malformed manifest, missing per-run secret,
    malformed guard) FAILS CLOSED via ``_hook_fail_closed`` (deny-JSON, exit 0) — a hook that
    cannot decide must never silently allow.

    The manifest path comes from ``CAIRN_HOOK_MANIFEST`` (baked absolute into the hook command by
    install_guards — a gatekeys-protected location OUTSIDE the run dir), falling back to
    ``guard_manifest_path(CAIRN_RUN_DIR, "hook")``; both resolve at hook-fire time because the hook
    inherits the executor's env."""
    try:
        raw_event = sys.stdin.read()
        event = json.loads(raw_event) if raw_event.strip() else None
        if not isinstance(event, dict):
            raise ValueError("PreToolUse event is not a JSON object")
    except (ValueError, OSError) as exc:
        return _hook_fail_closed(f"cairn hook: unreadable PreToolUse event ({exc})")

    tool, command = _hook_command(event)
    if command is None:
        return 0  # no command string → no command guard applies → allow

    run_dir = Path(os.environ.get("CAIRN_RUN_DIR", os.getcwd()))
    manifest_path = os.environ.get("CAIRN_HOOK_MANIFEST")
    if not manifest_path:
        manifest_path = str(guard_manifest_path(run_dir, "hook"))
    loaded = _load_verified_manifest(manifest_path, run_dir)
    if loaded is None:
        return _hook_fail_closed("cairn hook: guard manifest missing or failed verification")
    guards_raw, workspace_dir = loaded

    try:
        result = _run_chain(
            names, guards_raw, tool=tool, command=command,
            workspace_dir=workspace_dir, run_dir=run_dir, env=dict(os.environ),
        )
    except Exception as exc:  # a malformed guard decl etc. must BLOCK, not wave the tool through
        return _hook_fail_closed(f"cairn hook: guard check failed ({exc})")

    if not result.allowed:
        # A deny is carried by the deny-JSON on STDOUT (proven-blocking; mirrors hookprobe's
        # ClaudeHookRecipe, which exits 0 and blocks via stdout). Exit 0: the verdict is the JSON.
        # No trailing newline — byte-for-byte the probe-verified deny output.
        sys.stdout.write(deny_json(result.reason or "denied"))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """The ``python -m cairn.kernel.guards`` guard-check entry, in two modes:

    * ``--shim-check NAME...`` — a PATH shim's body: reads the command from ``CAIRN_SHIM_*`` env,
      exits 0 (allow → shim execs the real binary) / 2 (deny, reason on stderr).
    * ``--hook-check NAME...`` — a claude ``PreToolUse`` hook's body: reads the event from stdin,
      allows silently (exit 0) or blocks by printing the deny-JSON to stdout (exit 0); FAILS
      CLOSED on any error (deny-JSON, exit 0). Both modes run the SAME chain (``_run_chain``) over
      an authenticated manifest.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    mode = args[0] if args else None
    names = args[1:]
    if mode == "--shim-check":
        return _shim_check(names)
    if mode == "--hook-check":
        return _hook_check(names)
    print(
        "usage: python -m cairn.kernel.guards (--shim-check|--hook-check) NAME...",
        file=sys.stderr,
    )
    return 2  # fail-closed: a malformed invocation must not wave a command through


if __name__ == "__main__":
    raise SystemExit(main())
