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
Stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def run(*argv: str, cwd: Path | None = None) -> str:
    """Run a command, failing loudly with its stderr; returns stripped stdout."""
    res = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()


def apply_edit(root: Path, prop: dict) -> str | None:
    """Apply one proposal inside the worktree; return a skip reason or None on success."""
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

    applied: list[str] = []
    skipped: list[dict] = []
    try:
        for prop in proposals:
            reason = apply_edit(worktree / ws_rel, prop)
            if reason is None:
                applied.append(prop["target"])
            else:
                skipped.append({"id": prop.get("id"), "reason": reason})
                print(f"self-improve: skipping {prop.get('id')!r}: {reason}", file=sys.stderr)

        if not applied:
            print("self-improve: every proposal failed to apply — refusing to open an empty PR.",
                  file=sys.stderr)
            return 1

        # Commit BY PATHSPEC — only the approved proposals' targets, nothing else.
        paths = [str(ws_rel / t) for t in applied]
        run("git", "-C", str(worktree), "add", "--", *paths)
        lines = [f"self-improve: promote {len(applied)} learning(s) into the workspace", ""]
        for prop in proposals:
            if prop["target"] in applied:
                lines.append(f"* [{prop['promotion']}] {prop['target']} — {prop['rationale']}")
        lines += ["", "Proposed by the self-improve pipeline; suggestions, not truth — review before merging."]
        run("git", "-C", str(worktree), "commit", "-m", "\n".join(lines), "--", *paths)

        run("git", "-C", str(worktree), "push", "-u", "origin", branch)

        body = "\n".join([
            "Automated proposals from the `self-improve` pipeline (approved at the run's gate).",
            "These are **suggestions, not truth** — review each edit before merging.", "",
            *(f"- **{p['promotion']}** `{p['target']}`: {p['rationale']}"
              for p in proposals if p["target"] in applied),
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
    finally:
        # Drop the temporary worktree; the branch (and the PR) survive.
        subprocess.run(["git", "-C", str(repo_root), "worktree", "remove", "--force", str(worktree)],
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
