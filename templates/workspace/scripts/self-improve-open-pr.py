#!/usr/bin/env python3
"""self-improve open-pr — apply approved proposals on a NEW branch and open a PR.

Usage (wired by pipelines/self-improve.yaml; runs only after the approve gate said yes):

    self-improve-open-pr.py <proposals.json> <pr-record.json>

HARD RULE (docs/TOOLING-AND-GROWTH.md §7 — keep this property if you customize):
proposals arrive as branches/PRs, NEVER as direct commits. The learning loop has write
access to suggestions, not to truth. This script therefore never touches the working
branch or the working tree: it stages everything in a TEMPORARY GIT WORKTREE on a fresh
`self-improve/<run-id>` branch, commits there BY PATHSPEC (only the proposals' targets),
pushes that branch, and opens the PR with `gh`. The human merges — or doesn't.

Environment (exported by the cairn walker for every step):
  CAIRN_WORKSPACE — the workspace dir (must live inside a git repo)
  CAIRN_RUN_DIR   — this run's dir (its basename names the branch)

Exit 0 with a pr-record receipt on success (including the "no proposals" no-op);
exit 1 with reasons on stderr when nothing could be applied or git/gh refuse.
Failure states: when NOTHING applies, the fresh branch is deleted again (nothing of
value on it); when the push or `gh pr create` fails AFTER the commit, the branch is
KEPT locally — the commit is valuable for a retry — and the error names it. The
temporary worktree is removed on every path. Stdlib only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


def run(*argv: str, cwd: Path | None = None) -> str:
    """Run a command, failing loudly with its stderr; returns stripped stdout."""
    try:
        res = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(f"{argv[0]!r} not found on PATH") from None
    if res.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()


# Mirrors validators/self-improve-proposals.py `_check_target` — ON PURPOSE, as defense
# in depth: the validator guards the normal flow, but the documented per-proposal veto
# (a human editing proposals.json while the approve gate is open) happens AFTER
# validation, so this script must independently refuse a target that could write
# outside the worktree. Keep the two in sync if you customize either.
_FORBIDDEN_ROOTS = ("runs", ".git")


def unsafe_target(root: Path, target: str) -> str | None:
    """A refusal reason when ``target`` may not be edited, else None."""
    if not isinstance(target, str) or not target:
        return "unsafe target: must be a non-empty string"
    p = PurePosixPath(target)
    if p.is_absolute() or (len(target) > 1 and target[1] == ":"):
        return f"unsafe target {target!r}: must be workspace-relative, not absolute"
    if ".." in p.parts:
        return f"unsafe target {target!r}: escapes the workspace (contains '..')"
    if p.parts[0] in _FORBIDDEN_ROOTS or target == ".env":
        return f"unsafe target {target!r}: protected path"
    # Final backstop: the RESOLVED path (symlinks and all) must stay under the worktree.
    resolved = (root / target).resolve()
    if not resolved.is_relative_to(root.resolve()):
        return f"unsafe target {target!r}: resolves outside the worktree"
    return None


# Everything a proposal must carry to be applied AND reported (commit message, PR
# body). Re-checked here — not only in the schema — because a human may delete keys,
# not just whole proposals, while editing proposals.json at the open gate.
_REQUIRED_FIELDS = ("id", "promotion", "target", "action", "text", "rationale")


def apply_edit(root: Path, prop: dict) -> str | None:
    """Apply one proposal inside the worktree; return a skip reason or None on success."""
    missing = [k for k in _REQUIRED_FIELDS if not prop.get(k)]
    if missing:
        return f"missing required field(s): {', '.join(missing)}"
    reason = unsafe_target(root, prop.get("target", ""))
    if reason is not None:
        return reason
    target = root / prop["target"]
    action = prop["action"]
    if action == "create":
        if target.exists():
            return f"create: {prop['target']} already exists"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(prop["text"], encoding="utf-8")
        return None
    if not target.is_file():
        return f"{action}: {prop['target']} does not exist"
    content = target.read_text(encoding="utf-8")
    if action == "append":
        joiner = "" if content.endswith("\n") else "\n"
        target.write_text(content + joiner + prop["text"], encoding="utf-8")
        return None
    if action == "replace":
        find = prop.get("find", "")
        if content.count(find) != 1:
            return f"replace: 'find' must occur exactly once in {prop['target']} (found {content.count(find)})"
        target.write_text(content.replace(find, prop["text"]), encoding="utf-8")
        return None
    return f"unknown action {action!r}"


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: self-improve-open-pr.py <proposals.json> <pr-record.json>", file=sys.stderr)
        return 1
    proposals_path, record_path = Path(sys.argv[1]), Path(sys.argv[2])
    workspace = Path(os.environ["CAIRN_WORKSPACE"]).resolve()
    run_id = Path(os.environ.get("CAIRN_RUN_DIR", "")).name or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    doc = json.loads(proposals_path.read_text(encoding="utf-8"))
    proposals = doc.get("proposals", [])
    if not proposals:
        record_path.write_text(
            json.dumps({"status": "nothing-to-apply", "branch": None, "pr_url": None,
                        "applied": [], "skipped": []}, indent=2) + "\n",
            encoding="utf-8",
        )
        print("self-improve: no proposals to apply — no branch, no PR.")
        return 0

    repo_root = Path(run("git", "-C", str(workspace), "rev-parse", "--show-toplevel"))
    ws_rel = workspace.relative_to(repo_root)

    # A fresh branch in a TEMPORARY worktree: the user's checkout — branch, index, and
    # working tree — is never touched. Suggestions, not truth.
    branch = f"self-improve/{run_id}"
    worktree = Path(tempfile.mkdtemp(prefix="self-improve-")) / "worktree"
    try:
        run("git", "-C", str(repo_root), "worktree", "add", "-b", branch, str(worktree))
    except RuntimeError:
        branch = f"{branch}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
        run("git", "-C", str(repo_root), "worktree", "add", "-b", branch, str(worktree))

    applied_props: list[dict] = []  # the exact proposals that applied (never index a skipped one)
    skipped: list[dict] = []
    delete_branch = False
    try:
        for prop in proposals:
            reason = apply_edit(worktree / ws_rel, prop)
            if reason is None:
                applied_props.append(prop)
            else:
                skipped.append({"id": prop.get("id"), "reason": reason})
                print(f"self-improve: skipping {prop.get('id')!r}: {reason}", file=sys.stderr)

        applied = [p["target"] for p in applied_props]
        if not applied:
            print("self-improve: every proposal failed to apply — refusing to open an empty PR.",
                  file=sys.stderr)
            delete_branch = True  # the branch carries nothing; drop it in the cleanup below
            return 1

        # Commit BY PATHSPEC — only the applied proposals' targets, nothing else.
        paths = list(dict.fromkeys(str(ws_rel / t) for t in applied))
        run("git", "-C", str(worktree), "add", "--", *paths)
        lines = [f"self-improve: promote {len(applied)} learning(s) into the workspace", ""]
        for prop in applied_props:
            lines.append(f"* [{prop['promotion']}] {prop['target']} — {prop['rationale']}")
        lines += ["", "Proposed by the self-improve pipeline; suggestions, not truth — review before merging."]
        run("git", "-C", str(worktree), "commit", "-m", "\n".join(lines), "--", *paths)

        try:
            run("git", "-C", str(worktree), "push", "-u", "origin", branch)

            body = "\n".join([
                "Automated proposals from the `self-improve` pipeline (approved at the run's gate).",
                "These are **suggestions, not truth** — review each edit before merging.", "",
                *(f"- **{p['promotion']}** `{p['target']}`: {p['rationale']}" for p in applied_props),
            ])
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as bf:
                bf.write(body)
                body_file = bf.name
            try:
                pr_url = run("gh", "pr", "create", "--head", branch,
                             "--title", f"self-improve: promote {len(applied)} learning(s)",
                             "--body-file", body_file, cwd=worktree)
            finally:
                os.unlink(body_file)
        except RuntimeError as exc:
            # The commit is valuable — KEEP the branch for a retry (see the header);
            # only the nothing-applied path deletes its branch.
            print(f"self-improve: push/PR failed: {exc}", file=sys.stderr)
            print(f"self-improve: branch {branch!r} is kept locally with the applied edits — "
                  f"fix the push/PR (auth, remote, gh install) and publish it yourself, or drop "
                  f"it with `git branch -D {branch}`; a re-run mints a fresh branch.",
                  file=sys.stderr)
            return 1
    finally:
        # Drop the temporary worktree and its mkdtemp parent; on success the branch
        # (and the PR) survive, while an all-fail run leaves no branch ref behind.
        subprocess.run(["git", "-C", str(repo_root), "worktree", "remove", "--force", str(worktree)],
                       capture_output=True, text=True)
        shutil.rmtree(worktree.parent, ignore_errors=True)
        if delete_branch:
            subprocess.run(["git", "-C", str(repo_root), "branch", "-D", branch],
                           capture_output=True, text=True)

    record_path.write_text(
        json.dumps({"status": "pr-opened", "branch": branch, "pr_url": pr_url,
                    "applied": applied, "skipped": skipped}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"self-improve: opened {pr_url} from branch {branch} ({len(applied)} edit(s)).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"self-improve: {exc}", file=sys.stderr)
        raise SystemExit(1)
