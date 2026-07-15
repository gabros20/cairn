"""The ``claude`` executor — Anthropic's Claude Code CLI, headless."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from cairn.executors._cli import CliExecutor
from cairn.executors.base import Capabilities, Invocation
from cairn.kernel.gatekeys import guard_manifest_path
from cairn.kernel.guards import write_manifest

# cairn guard tool → claude PreToolUse matcher. cairn's guards are command guards; the real case
# is bash → the claude ``Bash`` tool (the shim layer covers the same command surface). A guard
# with no ``match_tool`` (applies to any tool) is installed under ``Bash`` — the only
# command-carrying tool a headless claude step drives.
_TOOL_TO_MATCHER = {"": "Bash", "bash": "Bash"}


class ClaudeExecutor(CliExecutor):
    name = "claude"
    _workspace_file = "CLAUDE.md"
    _install_hint = "install Claude Code and run `claude login`"
    capabilities = Capabilities(
        blocking_hooks=True,  # PreToolUse hooks can block (deny-JSON) — installed by install_guards
        output_schema=False,
        # W4: --no-session-persistence (below) makes this genuinely None, not just unconsumed —
        # claude-F7. No code ever read this glob; the transcripts it pointed at now don't exist.
        session_capture=None,
        installs_hooks=True,  # install_guards below actually wires the PreToolUse hook (W3a)
    )

    def _build_command(self, inv: Invocation, prompt_text: str) -> tuple[list[str], str | None]:
        # re-verify against `claude --help` at doctor time; vendors drift.
        # W4 (claude-F2/F6): the positional `prompt` arg is dropped from argv — `-p`/`--print`
        # reads it from stdin when absent. The captured help documents -p/--print but doesn't
        # itself spell out the stdin-fallback, so this is LIVE-VERIFIED, not just help-inferred:
        # `printf '<prompt>' | claude -p --model haiku --output-format text` returned the
        # expected reply with exit 0 (.orchestrate/raw/W4-claude-stdin-smoke.err is empty).
        # Keeps the envelope off `ps`/`/proc/*/cmdline` and off the argv `MAX_ARG_STRLEN`
        # (128 KiB) ceiling that a skill-heavy envelope could trip.
        argv = ["claude", "-p", "--model", inv.model]
        if inv.effort is not None:
            argv += ["--effort", inv.effort]
        argv += ["--output-format", "text"]
        # Headless `claude -p` under the default permission mode refuses every tool use
        # ("I need your permission to write the file.") and exits 0 without producing the
        # artifact, which then fails validation. cairn's guards (blocking PreToolUse hooks —
        # Capabilities.blocking_hooks, installed by install_guards) are the enforcement layer, so
        # run claude fully non-interactive and let the guards, not an interactive prompt, gate tools.
        argv += ["--permission-mode", "bypassPermissions"]
        # W4 config isolation (claude's half of F5): seal the process from ambient user config so
        # identical pipeline runs are deterministic. `--setting-sources project` drops the user's
        # `~/.claude/settings.json` and any local override while KEEPING the run-dir
        # `.claude/settings.json` install_guards writes (that IS the "project" source, cwd is the
        # run dir) — the cairn guard hook stays authoritative, only the user's ambient settings
        # go away. `--strict-mcp-config` drops any ambient MCP servers (none are passed via
        # --mcp-config, so this yields zero MCP servers rather than the user's set).
        argv += ["--setting-sources", "project", "--strict-mcp-config"]
        # W4 (claude-F7): retire the dead session-capture path — full transcripts otherwise
        # accumulate under ~/.claude/projects/, outside the run dir, and nothing ever read them
        # back in. "Disable session persistence … only works with --print" (captured help).
        argv += ["--no-session-persistence"]
        return argv, prompt_text  # prompt delivered on stdin, not argv

    def install_guards(self, guards, workspace_dir, run_dir) -> None:
        """Install the ``hook``-enforced guards as a claude ``PreToolUse`` hook.

        claude reads project settings from ``<cwd>/.claude/settings.json`` and cwd is the run
        dir, so we write ``<run_dir>/.claude/settings.json`` with a ``PreToolUse`` array whose
        hook command invokes ``python -m cairn.kernel.guards --hook-check`` (same guard chain the
        shim layer runs). Under ``bypassPermissions`` this hook is what actually blocks a guarded
        tool (proven by ``cairn doctor --probe-hooks`` / hookprobe.ClaudeHookRecipe). Idempotent;
        MERGES into any existing settings.json without clobbering unrelated keys. No hook-enforced
        guards → writes nothing.

        The guard manifest is written to a gatekeys-protected location OUTSIDE the run dir
        (``guard_manifest_path``) and SIGNED (per-run MAC + per-check-script hash), so the
        sandboxed agent cannot rewrite the policy its own commands are checked against; its
        absolute path is baked into the hook command and ``--hook-check`` authenticates it.

        NOTE (W3c): ``settings.json`` itself lives in the run dir, so under ``bypassPermissions``
        (whole-FS write) the agent can still rewrite/remove it or the per-run secret. That residual
        — an OS FS-sandbox or tool-scoping so claude cannot write/read outside the run dir — is
        tracked as future work W3c; the signed manifest here DETECTS tamper (fail closed) short of
        the agent stealing the secret, and ``post`` remains the hard gate.
        """
        hook_guards = [g for g in guards if "hook" in g.enforce]
        if not hook_guards:
            return None

        run_dir = Path(run_dir)
        workspace_dir = Path(workspace_dir)

        # The hook manifest — SIGNED, and written OUTSIDE the run dir (agent-unwritable under a
        # workspace-write sandbox). The SAME builder (write_manifest) the shim layer uses, so
        # --hook-check and --shim-check authenticate + re-load guards identically.
        manifest_path = guard_manifest_path(run_dir, "hook")
        write_manifest(
            hook_guards, workspace_dir=workspace_dir, run_dir=run_dir, path=manifest_path
        )

        # Merge our PreToolUse entries into settings.json, preserving unrelated keys and any
        # foreign (non-cairn) hook entries. Read-modify-write; a fresh run dir has no file yet.
        settings_path = run_dir / ".claude" / "settings.json"
        settings = _read_settings(settings_path)
        hooks = settings.setdefault("hooks", {})
        existing = hooks.get("PreToolUse", [])
        foreign = [e for e in existing if not _is_cairn_hook_entry(e)]
        hooks["PreToolUse"] = foreign + _pretooluse_entries(hook_guards, manifest_path)
        _write_if_changed(settings_path, json.dumps(settings, indent=2) + "\n")
        return None


# The marker that identifies OUR hook entries in a shared settings.json, so a re-install replaces
# them (idempotent) rather than appending duplicates, while leaving foreign entries untouched.
_HOOK_CHECK_MARKER = "cairn.kernel.guards --hook-check"


def _pretooluse_entries(guards, manifest_path: Path) -> list[dict]:
    """Group ``guards`` by claude matcher and build one ``PreToolUse`` entry per matcher, each
    running ``--hook-check`` over that matcher's guard names."""
    by_matcher: dict[str, list] = {}
    for g in guards:
        matcher = _TOOL_TO_MATCHER.get(g.match_tool, "Bash")
        by_matcher.setdefault(matcher, []).append(g)
    entries = []
    for matcher in sorted(by_matcher):
        names = [g.name for g in by_matcher[matcher]]
        entries.append(
            {
                "matcher": matcher,
                "hooks": [{"type": "command", "command": _hook_command(manifest_path, names)}],
            }
        )
    return entries


def _hook_command(manifest_path: Path, names: list[str]) -> str:
    """The shell command string claude runs as the hook. The manifest path is baked ABSOLUTE (as
    a ``CAIRN_HOOK_MANIFEST=`` env prefix — claude runs hook commands through a shell) so it
    resolves at hook-fire time regardless of the env claude passes. ``sys.executable`` runs the
    same interpreter the run uses."""
    prefix = f"CAIRN_HOOK_MANIFEST={shlex.quote(str(manifest_path))}"
    argv = [shlex.quote(sys.executable), "-m", "cairn.kernel.guards", "--hook-check"]
    argv += [shlex.quote(n) for n in names]
    return f"{prefix} {' '.join(argv)}"


def _is_cairn_hook_entry(entry: object) -> bool:
    """Does a PreToolUse entry belong to cairn (any hook command invoking ``--hook-check``)?"""
    if not isinstance(entry, dict):
        return False
    for hook in entry.get("hooks", []) or []:
        if isinstance(hook, dict) and _HOOK_CHECK_MARKER in str(hook.get("command", "")):
            return True
    return False


def _read_settings(path: Path) -> dict:
    """Load an existing settings.json (for a defensive read-modify-write); ``{}`` if missing.
    A settings file we cannot parse is treated as absent — we never crash the run over it, and
    we own the run dir, so overwriting a corrupt file there is acceptable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_if_changed(path: Path, body: str) -> None:
    """Write ``body`` only when missing or changed (idempotent — a second install is a no-op)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == body:
        return
    path.write_text(body, encoding="utf-8")
