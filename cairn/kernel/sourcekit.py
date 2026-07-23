"""``cairn new source <provider>`` — puller/fix scaffolds for source adapters (W4).

Generates a worked (github-issues) or seam (linear/jira/notion) source adapter into
an existing workspace: pull + fix pipelines, pull/refresh/notify scripts, and
printable triggers.yaml / schedules.yaml snippets with ship-gate defaults baked in
(SG1 identity:strict, SG4 explicit lease, SG5 fail-closed markers).

See docs/TRIGGERS.md § Source puller contract and docs/FACTORY-PLAN.md §7 W4.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent


# --------------------------------------------------------------------------- #
# Provider registry
# --------------------------------------------------------------------------- #

KNOWN_PROVIDERS = ("github-issues", "linear", "jira", "notion")


@dataclass(frozen=True)
class ProviderSpec:
    """One source provider the generator knows about."""

    name: str  # CLI choice, e.g. github-issues
    source: str  # identity-strict source segment ([a-z][a-z0-9]*)
    token_env: str  # env var name (value never scaffolded)
    token_env_alt: str | None  # optional alternate env name
    worked: bool  # True = real provider calls; False = # TODO seam
    endpoint_doc: str  # header comment documenting the API
    default_prio: int = 5
    inbox_max: int = 50
    lease: str = "60m"
    overlap_seconds: int = 60
    poll_cron: str = "*/5 * * * *"


_PROVIDERS: dict[str, ProviderSpec] = {
    "github-issues": ProviderSpec(
        name="github-issues",
        source="github",
        token_env="GH_TOKEN",
        token_env_alt="GITHUB_TOKEN",
        worked=True,
        endpoint_doc=(
            "GitHub Issues via `gh issue list --json number,title,url,createdAt,"
            "updatedAt,state,labels,body,author` (needs `gh` on PATH + GH_TOKEN "
            "or GITHUB_TOKEN). Scope is the current `gh` repo (GH_REPO / git remote)."
        ),
    ),
    "linear": ProviderSpec(
        name="linear",
        source="linear",
        token_env="LINEAR_TOKEN",
        token_env_alt=None,
        worked=False,
        endpoint_doc=(
            "Linear GraphQL API: POST https://api.linear.app/graphql with header "
            "Authorization: <LINEAR_TOKEN>. Query issues updated after the cursor "
            "watermark; normalize to the shape fetch_items returns."
        ),
    ),
    "jira": ProviderSpec(
        name="jira",
        source="jira",
        token_env="JIRA_TOKEN",
        token_env_alt=None,
        worked=False,
        endpoint_doc=(
            "Jira Cloud REST: GET https://<site>.atlassian.net/rest/api/3/search "
            "with Basic/Bearer auth from JIRA_TOKEN (+ JIRA_EMAIL / JIRA_SITE). "
            "JQL order by updated ASC; normalize to the shape fetch_items returns."
        ),
    ),
    "notion": ProviderSpec(
        name="notion",
        source="notion",
        token_env="NOTION_TOKEN",
        token_env_alt=None,
        worked=False,
        endpoint_doc=(
            "Notion API: POST https://api.notion.com/v1/databases/<id>/query with "
            "Authorization: Bearer <NOTION_TOKEN> and Notion-Version header. "
            "Filter/sort by last_edited_time; normalize to the shape fetch_items returns."
        ),
    ),
}


def provider_spec(name: str) -> ProviderSpec:
    """Return the provider spec or raise ValueError for an unknown name."""
    if name not in _PROVIDERS:
        known = ", ".join(KNOWN_PROVIDERS)
        raise ValueError(f"unknown source provider {name!r}; choose one of: {known}")
    return _PROVIDERS[name]


def _paths(provider: str) -> dict[str, str]:
    return {
        "pull_pipeline": f"pipelines/pull-{provider}.yaml",
        "fix_pipeline": f"pipelines/fix-{provider}.yaml",
        "pull_script": f"scripts/pull_{provider}.py",
        "refresh_script": f"scripts/refresh_{provider}.py",
        "notify_script": f"scripts/notify_{provider}.py",
    }


def _sub(template: str, mapping: dict[str, str]) -> str:
    """Replace ``__KEY__`` placeholders. Values are inserted raw (pre-escaped).

    Longer keys are applied first so ``__PROVIDER_REPR__`` is not partially
    eaten by a shorter ``__PROVIDER__`` replacement.
    """
    out = template
    for key in sorted(mapping.keys(), key=len, reverse=True):
        out = out.replace(f"__{key}__", mapping[key])
    return out


# --------------------------------------------------------------------------- #
# Public entry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceScaffoldResult:
    """What ``new_source`` wrote and what the operator must still paste."""

    provider: str
    files: tuple[str, ...]
    triggers_snippet: str
    schedules_snippet: str
    notes: tuple[str, ...]


def new_source(provider: str, workspace_dir: Path) -> SourceScaffoldResult:
    """Instantiate a source adapter for ``provider`` into ``workspace_dir``.

    Refuses an unknown provider (:class:`ValueError`) or a pre-existing file
    among the generated set (:class:`FileExistsError`) — same posture as
    :func:`new_stub`. Never edits the user's ``triggers.yaml`` /
    ``schedules.yaml``; returns printable snippets instead (self-improve pattern).
    """
    spec = provider_spec(provider)
    workspace_dir = Path(workspace_dir)
    if not workspace_dir.is_dir():
        raise FileNotFoundError(f"workspace directory does not exist: {workspace_dir}")

    paths = _paths(provider)
    existing = [rel for rel in paths.values() if (workspace_dir / rel).exists()]
    if existing:
        raise FileExistsError(
            f"refusing to overwrite existing source files: {', '.join(existing)}"
        )

    bodies = {
        paths["pull_pipeline"]: _pull_pipeline(spec),
        paths["fix_pipeline"]: _fix_pipeline(spec),
        paths["pull_script"]: _pull_script(spec),
        paths["refresh_script"]: _refresh_script(spec),
        paths["notify_script"]: _notify_script(spec),
    }
    written: list[str] = []
    for rel, body in bodies.items():
        dest = workspace_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
        if rel.endswith(".py"):
            dest.chmod(0o755)
        written.append(rel)

    (workspace_dir / "work" / "inbox").mkdir(parents=True, exist_ok=True)
    (workspace_dir / "state").mkdir(parents=True, exist_ok=True)

    token_note = spec.token_env
    if spec.token_env_alt:
        token_note += f" (or {spec.token_env_alt})"
    notes = (
        f"provider={spec.name} source={spec.source} "
        f"({'worked reference' if spec.worked else 'scaffold seam — fill the TODO API call'})",
        f"token env: {token_note} — never commit a value",
        "triggers.yaml / schedules.yaml are NOT edited — paste the snippets below",
        f"ship-gates: identity:strict (SG1), lease: {spec.lease} (SG4), "
        "fail-closed notify markers (SG5)",
        "SG6: ledger invariant audit QUARANTINES settled violations to "
        ".quarantine/ (never auto-deletes); in-grace claims left alone",
        "W8 worktree pattern: fix pipeline sets worktree: true — implement in a "
        "per-run git worktree, record path on run.json, lock only deliver",
    )
    return SourceScaffoldResult(
        provider=provider,
        files=tuple(written),
        triggers_snippet=_triggers_snippet(spec),
        schedules_snippet=_schedules_snippet(spec),
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Pipelines
# --------------------------------------------------------------------------- #


def _pull_pipeline(spec: ProviderSpec) -> str:
    return _sub(
        dedent(
            """\
            # pull-__PROVIDER__ — source puller: poll → work/inbox work-items + poll-report.
            # Scaffolded by `cairn new source __PROVIDER__`. Cursor advances ONLY when the
            # poll-report validates complete:true (validators/poll-report-complete.py).
            # Backpressure: the pull script skips the poll when work/inbox depth ≥
            # inbox_max (default __INBOX_MAX__) and leaves the cursor untouched.
            #
            # Env: __TOKEN_DOC__
            # Pair with the schedules.yaml snippet printed by `cairn new source`.
            pipeline: pull-__PROVIDER__
            version: 1

            params:
              inbox_max: { type: int, default: __INBOX_MAX__ }

            run_id: "pull-__PROVIDER__-{date}"

            artifacts:
              poll-report:
                path: poll-report.json
                schema: schemas/poll-report.json
                validator: validators/poll-report-complete.py
                describe: >-
                  Completeness receipt for this poll. complete:false fails validation
                  so the kernel withholds the cursor commit (rate-limit / pagination).
                  Backpressure pause uses complete:true + paused:true and does not
                  rewrite cursor.next (watermark untouched, exit 0).

            steps:
              - step: poll
                run: >-
                  python3 "$CAIRN_WORKSPACE/scripts/pull___PROVIDER__.py"
                  "{cursor.value}" "{cursor.next}" "{artifact:poll-report}"
                  --inbox-max "{params.inbox_max}"
                cursor: state/__PROVIDER__.cursor
                produces: [poll-report]
            """
        ),
        {
            "PROVIDER": spec.name,
            "INBOX_MAX": str(spec.inbox_max),
            "TOKEN_DOC": _token_doc(spec),
        },
    )


def _fix_pipeline(spec: ProviderSpec) -> str:
    return _sub(
        dedent(
            """\
            # fix-__PROVIDER__ — consume one work-item from the trigger inbox, refresh
            # upstream status (T6b), do the work, deliver via fail-closed notify (SG5).
            # Scaffolded by `cairn new source __PROVIDER__`.
            #
            # WORKTREE PATTERN (W8-T2 — furniture, not kernel git-wrapping):
            #   worktree: true marks this pipeline as per-run-worktree. Implement
            #   steps work in an isolated linked worktree (no shared repo: lock).
            #   Only deliver takes locks: [repo:.] for the shared-refs op (push /
            #   branch / PR). Mirror self-improve-open-pr.py:
            #     1. git worktree add -b <branch> <path>   (keyed by work-item id)
            #     2. record path: cairn.kernel.runstate.record_worktree(run_dir, path)
            #     3. edit only under the worktree (safe-target-under-worktree check)
            #     4. deliver under locks: [repo:.] ; gc prunes the worktree later
            #   Cairn provides the lease + run.json worktree field + gc prune;
            #   the scaffold does the git (D5).
            #
            # The work step is a PLACEHOLDER run: — replace with agent: or a real
            # command once the adapter is wired. A closed source-status skips work
            # and delivery (run completes without a delivery artifact).
            #
            # Fired by the triggers.yaml work-queue entry (identity:strict + lease).
            pipeline: fix-__PROVIDER__
            version: 1
            worktree: true

            params:
              event: { type: string, required: true }  # absolute path of the claimed inbox file

            run_id: "fix-__PROVIDER__-{date}"

            artifacts:
              work-item:
                path: work-item.json
                schema: schemas/work-item.json
                describe: "Schema-validated copy of the claimed inbox work-item."
              source-status:
                path: source-status.json
                schema: schemas/source-status.json
                validator: validators/source-status.py
                describe: "T6b refresh: current | changed | closed."
              delivery:
                path: delivery.json
                validator: validators/nonempty.py
                describe: "Receipt from the notify script (PR/comment/label marker)."

            steps:
              # 1. intake — copy the claimed file into a schema-validated artifact.
              - step: intake
                run: >-
                  python3 -c "import json,sys,pathlib;
                  src=pathlib.Path(sys.argv[1]); dst=pathlib.Path(sys.argv[2]);
                  doc=json.loads(src.read_text(encoding='utf-8'));
                  dst.write_text(json.dumps(doc, indent=2)+chr(10), encoding='utf-8')"
                  "{params.event}" "{artifact:work-item}"
                produces: [work-item]

              # 2. refresh — T6b: is upstream still current / changed / closed?
              - step: refresh
                run: >-
                  python3 "$CAIRN_WORKSPACE/scripts/refresh___PROVIDER__.py"
                  "{artifact:work-item}" "{artifact:source-status}"
                needs: [work-item]
                produces: [source-status]

              # 3. work — PLACEHOLDER. Replace with agent: or a real run: command.
              #    Worktree pattern: create/record a per-run worktree (no repo: lock —
              #    isolated working tree). Skipped when upstream is closed.
              - step: work
                when: "artifacts.source-status.status != 'closed'"
                run: >-
                  python3 -c "import pathlib,sys;
                  pathlib.Path(sys.argv[1]).write_text(
                    'TODO: implement fix work for this source in a per-run git worktree\\n'
                    '(git worktree add → record_worktree → edit under worktree only)\\n',
                    encoding='utf-8')"
                  "$CAIRN_RUN_DIR/work-notes.txt"
                needs: [source-status]

              # 4. deliver — fail-closed notify (SG5). SHARED-REFS step: holds
              #    locks: [repo:.] so concurrent deliverers serialize on the main
              #    repo. Skipped when closed.
              - step: deliver
                when: "artifacts.source-status.status != 'closed'"
                locks: [repo:.]
                run: >-
                  python3 "$CAIRN_WORKSPACE/scripts/notify___PROVIDER__.py"
                  "{artifact:work-item}" "{artifact:delivery}"
                needs: [work-item, source-status]
                produces: [delivery]

              # 5. cancel-closed — complete the run without delivery when upstream closed.
              - step: cancel-closed
                when: "artifacts.source-status.status == 'closed'"
                run: >-
                  python3 -c "import sys; print('upstream closed — no delivery')"
                needs: [source-status]
            """
        ),
        {"PROVIDER": spec.name},
    )


def _token_doc(spec: ProviderSpec) -> str:
    if spec.token_env_alt:
        return f"{spec.token_env} / {spec.token_env_alt}"
    return spec.token_env


def _token_keys_expr(spec: ProviderSpec) -> str:
    """Python expression listing token env keys for a for-loop."""
    keys = [spec.token_env]
    if spec.token_env_alt:
        keys.append(spec.token_env_alt)
    return "(" + ", ".join(repr(k) for k in keys) + ("," if len(keys) == 1 else "") + ")"


def _missing_token_msg(spec: ProviderSpec) -> str:
    if spec.token_env_alt:
        return f"missing token env {spec.token_env} or {spec.token_env_alt} — set it in the environment, never commit a value"
    return f"missing token env {spec.token_env} — set it in the environment, never commit a value"


# --------------------------------------------------------------------------- #
# Pull script
# --------------------------------------------------------------------------- #


_PULL_COMMON = r'''#!/usr/bin/env python3
"""pull___PROVIDER__ — poll __PROVIDER__ into work/inbox/ with cursor + backpressure.

Scaffolded by `cairn new source __PROVIDER__`.

__ENDPOINT_DOC__

Env (tokens ONLY via env — never a scaffolded value):
  __TOKEN_DOC__
  CAIRN_WORKSPACE — set by the cairn walker

Usage (wired by pipelines/pull-__PROVIDER__.yaml):
  pull___PROVIDER__.py <cursor_value> <cursor_next_path> <poll_report_path> [--inbox-max N]

Cursor value is the committed watermark text ("" on first run) — JSON
{"updated_at": "...", "id": "..."} when present. Cursor next is a scratch
path the script writes the candidate watermark to; the kernel commits it only
after poll-report validates complete:true.

Backpressure: if work/inbox depth >= inbox_max, skip the poll, emit a paused
poll-report (complete:true, paused:true, emitted:0), leave cursor.next
untouched, exit 0.

SG6: ledger invariant audit QUARANTINES settled violations to `.quarantine/`
(never auto-deletes; in-grace claims left alone). Pullers still run under
identity:strict + fail-closed markers; operator watches reconcile/doctor for
quarantine counts.

Stdlib + cairn.kernel.work_item; pure helpers below are unit-tested with a
seamed fetch_items (no live network in tests).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

try:
    from cairn.kernel.work_item import (
        clamp_prio as _clamp_prio,
        safe_item_id as _safe_item_id,
        safe_source as _safe_source,
        work_item_filename as _work_item_filename,
        work_item_rev as _work_item_rev,
    )
except ImportError:  # pragma: no cover
    def _work_item_rev(updated_at: str, version: int | None = None) -> str:
        raise RuntimeError("cairn.kernel.work_item.work_item_rev is required")

    def _safe_item_id(raw: object) -> str:
        raise RuntimeError("cairn.kernel.work_item.safe_item_id is required")

    def _safe_source(raw: object) -> str:
        raise RuntimeError("cairn.kernel.work_item.safe_source is required")

    def _clamp_prio(prio: object) -> int:
        raise RuntimeError("cairn.kernel.work_item.clamp_prio is required")

    def _work_item_filename(prio, source, item_id, rev) -> str:
        raise RuntimeError("cairn.kernel.work_item.work_item_filename is required")


# Re-export kernel helpers so tests / customizations import from this script.
safe_item_id = _safe_item_id
work_item_filename = _work_item_filename
clamp_prio = _clamp_prio

SOURCE = __SOURCE_REPR__
PROVIDER = __PROVIDER_REPR__
DEFAULT_PRIO = __DEFAULT_PRIO__
DEFAULT_INBOX_MAX = __INBOX_MAX__
OVERLAP_SECONDS = __OVERLAP__
TOKEN_ENV = __TOKEN_ENV_REPR__
TOKEN_ENV_ALT = __TOKEN_ENV_ALT_REPR__
# Bound hung provider calls so a stuck gh cannot hang the poll forever.
GH_TIMEOUT_S = 30


def parse_cursor(raw: str) -> dict[str, str] | None:
    """Parse cursor watermark text -> {updated_at, id} or None when empty/missing."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict):
        return None
    ua, iid = doc.get("updated_at"), doc.get("id")
    if not isinstance(ua, str) or not isinstance(iid, str) or not ua or not iid:
        return None
    return {"updated_at": ua, "id": iid}


def format_cursor(updated_at: str, item_id: str) -> str:
    """Serialize a cursor watermark (stable key order)."""
    return json.dumps({"updated_at": updated_at, "id": item_id}, separators=(",", ":"))


def overlap_since(cursor: dict[str, str] | None, overlap_seconds: int = OVERLAP_SECONDS) -> str | None:
    """ISO-8601 lower bound slightly before the watermark (overlap window)."""
    if cursor is None:
        return None
    ua = cursor["updated_at"]
    s = ua.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return ua
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    earlier = dt - timedelta(seconds=overlap_seconds)
    return earlier.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def inbox_depth(inbox_dir: Path) -> int:
    """Count top-level non-dot files in the inbox (spool depth for backpressure)."""
    if not inbox_dir.is_dir():
        return 0
    n = 0
    for p in inbox_dir.iterdir():
        if p.name.startswith("."):
            continue
        if p.is_file():
            n += 1
    return n


def should_skip_for_backpressure(depth: int, inbox_max: int) -> bool:
    """True when spool depth is at/over the cap — skip the poll, leave cursor."""
    if inbox_max < 1:
        return False
    return depth >= inbox_max


def build_work_item(
    *,
    item_id: str,
    title: str,
    url: str,
    created: str,
    updated_at: str,
    payload: dict[str, Any],
    prio: int = DEFAULT_PRIO,
    source: str = SOURCE,
    rev_fn: Callable[[str], str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build (filename, body) for one upstream item.

    Untrusted ids go through cairn.kernel.work_item.safe_item_id /
    work_item_filename — never hand-rolled .lower(). A fully-unsalvageable id
    raises ValueError (caller skips the item; does not wedge the pull).
    """
    rev_fn = rev_fn or _work_item_rev
    rev_token = rev_fn(updated_at)
    safe_id = _safe_item_id(item_id)
    safe_src = _safe_source(source)
    safe_prio = _clamp_prio(prio)
    rev_digits = rev_token[1:] if rev_token.startswith("r") else rev_token
    body = {
        "id": safe_id,
        "source": safe_src,
        "title": title,
        "url": url,
        "prio": safe_prio,
        "created": created,
        "updated_at": updated_at,
        "rev": rev_digits,
        "payload": payload,
    }
    name = _work_item_filename(safe_prio, safe_src, safe_id, rev_token)
    return name, body


def paused_poll_report(
    source: str, cursor: dict[str, str] | None, *, reason: str = "inbox_max"
) -> dict[str, Any]:
    """Backpressure skip report: complete so the step exits 0; no cursor advance."""
    cur = cursor or {"updated_at": "", "id": ""}
    return {
        "complete": True,
        "paused": True,
        "pause_reason": reason,
        "cursor": cur,
        "emitted": 0,
        "source": source,
    }


def complete_poll_report(
    source: str,
    cursor: dict[str, str],
    emitted: int,
    *,
    complete: bool = True,
) -> dict[str, Any]:
    return {
        "complete": complete,
        "cursor": cursor,
        "emitted": int(emitted),
        "source": source,
    }


def read_token(environ: Mapping[str, str] | None = None) -> str:
    """Read the provider token from the environment (never from disk)."""
    env = environ if environ is not None else os.environ
    for key in __TOKEN_KEYS__:
        val = env.get(key, "").strip()
        if val:
            return val
    raise RuntimeError(__MISSING_TOKEN_MSG_REPR__)


NormalizedItem = dict[str, Any]


__FETCH_BODY__


def run_pull(
    *,
    workspace: Path,
    cursor_value: str,
    cursor_next: Path,
    poll_report_path: Path,
    inbox_max: int = DEFAULT_INBOX_MAX,
    fetch: Callable[..., list] | None = None,
    rev_fn: Callable[[str], str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Core pull pipeline. Returns process exit code."""
    inbox = workspace / "work" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    cursor = parse_cursor(cursor_value)
    depth = inbox_depth(inbox)
    if should_skip_for_backpressure(depth, inbox_max):
        report = paused_poll_report(SOURCE, cursor)
        poll_report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return 0

    fetch = fetch or fetch_items
    since = overlap_since(cursor)
    try:
        items = fetch(since=since, cursor=cursor, environ=environ)
    except Exception as exc:
        report = complete_poll_report(
            SOURCE,
            cursor or {"updated_at": "", "id": ""},
            0,
            complete=False,
        )
        report["error"] = str(exc)
        poll_report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return 0

    emitted = 0
    skipped = 0
    skip_reasons: list[str] = []
    # dest basename → first raw upstream id that claimed it this run (id-collision detect)
    claimed_dest: dict[str, str] = {}
    high_ua = cursor["updated_at"] if cursor else ""
    high_id = cursor["id"] if cursor else ""
    inbox_resolved = inbox.resolve()
    for raw in items:
        raw_id = raw.get("id") if isinstance(raw, dict) else None
        try:
            if not isinstance(raw, dict):
                raise ValueError(f"item is not an object: {type(raw).__name__}")
            item_id = str(raw["id"])
            updated_at = str(raw["updated_at"])
            name, body = build_work_item(
                item_id=item_id,
                title=str(raw.get("title") or ""),
                url=str(raw.get("url") or ""),
                created=str(raw.get("created") or updated_at),
                updated_at=updated_at,
                payload=dict(raw.get("payload") or {}),
                prio=int(raw.get("prio") if raw.get("prio") is not None else DEFAULT_PRIO),
                source=SOURCE,
                rev_fn=rev_fn,
            )
            # Basename only after safe_item_id; belt-and-braces resolve check.
            if "/" in name or "\\" in name or name in (".", ".."):
                raise ValueError(f"refusing path metachar in filename {name!r}")
            dest = inbox / name
            if not dest.resolve().is_relative_to(inbox_resolved):
                raise ValueError(f"refusing path escape for filename {name!r}")
            if not dest.exists():
                dest.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
                emitted += 1
                claimed_dest[name] = item_id
            else:
                # Dest already present. Same raw id (overlap re-fetch) is an
                # intentional no-op; a DIFFERENT raw id that sanitized to the
                # same token is a rare collision — count + reason so it is not
                # a silent missed-emit (safety unchanged: still do not overwrite).
                prior_raw = claimed_dest.get(name)
                if prior_raw is None:
                    claimed_dest[name] = item_id
                elif prior_raw != item_id:
                    skipped += 1
                    reason = (
                        f"{item_id!r}: id-collision dest={name} "
                        f"already claimed by {prior_raw!r}"
                    )
                    skip_reasons.append(reason)
                    print(f"pull: skip item {reason}", file=sys.stderr)
            # High-water uses the sanitized id (body["id"]), never the raw upstream string.
            safe_id = body["id"]
            if (not high_ua) or updated_at > high_ua or (
                updated_at == high_ua and safe_id > high_id
            ):
                high_ua, high_id = updated_at, safe_id
        except Exception as exc:
            # One poison item must not wedge the pull (no cursor advance on crash,
            # no missing poll-report). Skip + diagnose; loop continues.
            skipped += 1
            reason = f"{raw_id!r}: {exc}"
            skip_reasons.append(reason)
            print(f"pull: skip item {reason}", file=sys.stderr)
            continue

    if not high_ua:
        new_cursor = cursor or {"updated_at": "", "id": ""}
    else:
        new_cursor = {"updated_at": high_ua, "id": high_id}

    report = complete_poll_report(SOURCE, new_cursor, emitted, complete=True)
    if skipped:
        report["skipped"] = skipped
        report["skip_reasons"] = skip_reasons[:20]  # bound size
    poll_report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if new_cursor.get("updated_at"):
        cursor_next.parent.mkdir(parents=True, exist_ok=True)
        cursor_next.write_text(
            format_cursor(new_cursor["updated_at"], new_cursor["id"]),
            encoding="utf-8",
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=f"pull {PROVIDER} into work/inbox")
    ap.add_argument("cursor_value", help="committed watermark text (may be empty)")
    ap.add_argument("cursor_next", type=Path, help="scratch path for candidate watermark")
    ap.add_argument("poll_report", type=Path, help="path for poll-report artifact")
    ap.add_argument("--inbox-max", type=int, default=DEFAULT_INBOX_MAX)
    ap.add_argument("--workspace", type=Path, default=None)
    args = ap.parse_args(argv)
    ws = args.workspace or Path(os.environ.get("CAIRN_WORKSPACE") or ".")
    return run_pull(
        workspace=ws,
        cursor_value=args.cursor_value,
        cursor_next=args.cursor_next,
        poll_report_path=args.poll_report,
        inbox_max=args.inbox_max,
    )


if __name__ == "__main__":
    raise SystemExit(main())
'''


_GITHUB_FETCH = r'''
def fetch_items(
    *,
    since: str | None,
    cursor: dict[str, str] | None,
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., str] | None = None,
) -> list[NormalizedItem]:
    """List GitHub issues via gh. runner is seamed for tests (no network)."""
    read_token(environ)

    def _run(argv: list[str]) -> str:
        if runner is not None:
            return runner(argv)
        import subprocess
        try:
            res = subprocess.run(
                argv, capture_output=True, text=True, check=False, timeout=GH_TIMEOUT_S
            )
        except FileNotFoundError as exc:
            raise RuntimeError("'gh' not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"gh timed out after {GH_TIMEOUT_S}s") from exc
        if res.returncode != 0:
            raise RuntimeError(f"gh failed: {res.stderr.strip() or res.stdout.strip()}")
        return res.stdout

    argv = [
        "gh", "issue", "list",
        "--state", "all",
        "--limit", "100",
        "--json", "number,title,url,createdAt,updatedAt,state,labels,body,author",
    ]
    if since:
        argv = [
            "gh", "issue", "list",
            "--state", "all",
            "--limit", "100",
            "--search", f"updated:>={since[:10]}",
            "--json", "number,title,url,createdAt,updatedAt,state,labels,body,author",
        ]
    raw = _run(argv)
    try:
        rows = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh returned non-JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise RuntimeError("gh issue list: expected a JSON array")

    out: list[NormalizedItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        num = row.get("number")
        if num is None:
            continue
        item_id = str(num)
        updated = str(row.get("updatedAt") or "")
        created = str(row.get("createdAt") or updated)
        if cursor and updated:
            if updated < cursor["updated_at"]:
                continue
            if updated == cursor["updated_at"] and item_id.lower() <= cursor["id"].lower():
                continue
        out.append({
            "id": item_id,
            "title": str(row.get("title") or ""),
            "url": str(row.get("url") or ""),
            "created": created,
            "updated_at": updated,
            "prio": DEFAULT_PRIO,
            "payload": {
                "state": row.get("state"),
                "labels": row.get("labels"),
                "body": row.get("body"),
                "author": row.get("author"),
                "number": num,
            },
        })
    out.sort(key=lambda r: (r["updated_at"], r["id"]))
    return out
'''


_SEAM_FETCH = r'''
def fetch_items(
    *,
    since: str | None,
    cursor: dict[str, str] | None,
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., str] | None = None,
) -> list[NormalizedItem]:
    """Fetch normalized items from __PROVIDER__.

    # TODO: your __PROVIDER__ API call here
    # __ENDPOINT_DOC__
    # Read the token with read_token(environ); never hardcode it.
    # Return a list of dicts with keys:
    #   id, title, url, created, updated_at, payload (dict), optional prio
    # Honor `since` (overlap lower bound) and skip items <= cursor watermark.
    # Raise on hard API failure so run_pull emits complete:false.
    #
    # Id safety: prefer grammar-safe ids ([a-z0-9]([a-z0-9._-]*[a-z0-9])?).
    # run_pull ALWAYS passes each id through cairn.kernel.work_item.safe_item_id
    # / work_item_filename — never hand-roll .lower(). Hostile or unsalvageable
    # ids are skipped per-item (diagnostic + skipped count); they must NOT wedge
    # the pull or path-traverse out of work/inbox/.
    """
    read_token(environ)
    _ = (since, cursor, runner)  # seam placeholders
    # --- begin provider seam -------------------------------------------------
    # TODO: your __PROVIDER__ API call here
    raise NotImplementedError(
        "__PROVIDER__ puller is a scaffold seam — implement fetch_items() "
        "(see the script header for endpoint + env docs)"
    )
    # --- end provider seam ---------------------------------------------------
'''


def _pull_script(spec: ProviderSpec) -> str:
    fetch = _GITHUB_FETCH if spec.worked else _sub(_SEAM_FETCH, {
        "PROVIDER": spec.name,
        "ENDPOINT_DOC": spec.endpoint_doc,
    })
    return _sub(
        _PULL_COMMON,
        {
            "PROVIDER": spec.name,
            "SOURCE_REPR": repr(spec.source),
            "PROVIDER_REPR": repr(spec.name),
            "DEFAULT_PRIO": str(spec.default_prio),
            "INBOX_MAX": str(spec.inbox_max),
            "OVERLAP": str(spec.overlap_seconds),
            "TOKEN_ENV_REPR": repr(spec.token_env),
            "TOKEN_ENV_ALT_REPR": repr(spec.token_env_alt),
            "TOKEN_DOC": _token_doc(spec),
            "TOKEN_KEYS": _token_keys_expr(spec),
            "MISSING_TOKEN_MSG_REPR": repr(_missing_token_msg(spec)),
            "ENDPOINT_DOC": spec.endpoint_doc,
            "FETCH_BODY": fetch,
        },
    )


# --------------------------------------------------------------------------- #
# Refresh script
# --------------------------------------------------------------------------- #


_REFRESH_COMMON = r'''#!/usr/bin/env python3
"""refresh___PROVIDER__ — T6b source-status for a claimed work-item.

Scaffolded by `cairn new source __PROVIDER__`.

Writes source-status.json with status in current|changed|closed (never a bare
skippable). Env: __TOKEN_DOC__.

Usage:
  refresh___PROVIDER__.py <work-item.json> <source-status.json>

Provider lookup is seamed via lookup_upstream() for tests (no live network).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

SOURCE = __SOURCE_REPR__
TOKEN_ENV = __TOKEN_ENV_REPR__


def read_token(environ: Mapping[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    for key in __TOKEN_KEYS__:
        val = env.get(key, "").strip()
        if val:
            return val
    raise RuntimeError(__MISSING_TOKEN_MSG_REPR__)


def classify_status(
    checked_rev: str,
    *,
    upstream_rev: str | None,
    closed: bool,
) -> dict[str, Any]:
    """Pure T6b classifier -> source-status body."""
    if closed:
        return {"status": "closed", "checked_rev": checked_rev}
    if upstream_rev is not None and upstream_rev != checked_rev:
        return {
            "status": "changed",
            "checked_rev": checked_rev,
            "upstream_rev": upstream_rev,
        }
    return {"status": "current", "checked_rev": checked_rev}


__LOOKUP_BODY__


def run_refresh(
    work_item_path: Path,
    status_path: Path,
    *,
    lookup: Callable[..., dict[str, Any]] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    doc = json.loads(work_item_path.read_text(encoding="utf-8"))
    checked_rev = str(doc.get("rev") or "")
    item_id = str(doc.get("id") or "")
    if not checked_rev or not item_id:
        print("work-item missing id/rev", file=sys.stderr)
        return 1
    lookup = lookup or lookup_upstream
    try:
        up = lookup(item_id=item_id, work_item=doc, environ=environ)
    except Exception as exc:
        print(f"refresh lookup failed: {exc}", file=sys.stderr)
        return 1
    body = classify_status(
        checked_rev,
        upstream_rev=up.get("rev"),
        closed=bool(up.get("closed")),
    )
    status_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print(
            "usage: refresh___PROVIDER__.py <work-item.json> <source-status.json>",
            file=sys.stderr,
        )
        return 1
    return run_refresh(Path(argv[0]), Path(argv[1]))


if __name__ == "__main__":
    raise SystemExit(main())
'''


_GITHUB_LOOKUP = r'''
def lookup_upstream(
    *,
    item_id: str,
    work_item: dict[str, Any],
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Return {rev, closed} for a GitHub issue. runner seamed for tests."""
    read_token(environ)

    def _run(argv: list[str]) -> str:
        if runner is not None:
            return runner(argv)
        import subprocess
        try:
            res = subprocess.run(
                argv, capture_output=True, text=True, check=False, timeout=30
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("gh timed out after 30s") from exc
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or res.stdout.strip() or "gh failed")
        return res.stdout

    raw = _run([
        "gh", "issue", "view", str(item_id),
        "--json", "number,state,updatedAt",
    ])
    row = json.loads(raw)
    state = str(row.get("state") or "").upper()
    closed = state in ("CLOSED", "DONE")
    updated = str(row.get("updatedAt") or "")
    try:
        from cairn.kernel.work_item import work_item_rev
        rev_token = work_item_rev(updated) if updated else ""
        rev = rev_token[1:] if rev_token.startswith("r") else rev_token
    except Exception:
        rev = str(work_item.get("rev") or "")
    return {"rev": rev, "closed": closed}
'''


_SEAM_LOOKUP = r'''
def lookup_upstream(
    *,
    item_id: str,
    work_item: dict[str, Any],
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Return {rev, closed} for a __PROVIDER__ item.

    # TODO: your __PROVIDER__ API call here
    # __ENDPOINT_DOC__
    """
    read_token(environ)
    _ = (item_id, work_item, runner)
    raise NotImplementedError(
        "__PROVIDER__ refresh is a scaffold seam — implement lookup_upstream()"
    )
'''


def _refresh_script(spec: ProviderSpec) -> str:
    lookup = _GITHUB_LOOKUP if spec.worked else _sub(_SEAM_LOOKUP, {
        "PROVIDER": spec.name,
        "ENDPOINT_DOC": spec.endpoint_doc,
    })
    return _sub(
        _REFRESH_COMMON,
        {
            "PROVIDER": spec.name,
            "SOURCE_REPR": repr(spec.source),
            "TOKEN_ENV_REPR": repr(spec.token_env),
            "TOKEN_DOC": _token_doc(spec),
            "TOKEN_KEYS": _token_keys_expr(spec),
            "MISSING_TOKEN_MSG_REPR": repr(_missing_token_msg(spec)),
            "LOOKUP_BODY": lookup,
        },
    )


# --------------------------------------------------------------------------- #
# Notify script (SG5 fail-closed)
# --------------------------------------------------------------------------- #


_NOTIFY_COMMON = r'''#!/usr/bin/env python3
"""notify___PROVIDER__ — write-back with FAIL-CLOSED idempotency markers (SG5).

Scaffolded by `cairn new source __PROVIDER__`.

Every external effect (PR / comment / label) is keyed on identity+rev via a
deterministic marker. Find-before-create:

  * authoritative ABSENT  -> create
  * authoritative PRESENT -> reuse (idempotent no-op)
  * UNCERTAIN (API error / timeout / cannot confirm absence) -> exit BLOCKED (9),
    NEVER create (fail closed, doctrine D4)

Env: __TOKEN_DOC__

Usage:
  notify___PROVIDER__.py <work-item.json> <delivery.json>

Exit codes: 0 ok, 1 hard failure, 9 BLOCKED (uncertainty — operator needed).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

SOURCE = __SOURCE_REPR__
PROVIDER = __PROVIDER_REPR__
EXIT_BLOCKED = 9
EXIT_FAIL = 1
EXIT_OK = 0


def read_token(environ: Mapping[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    for key in __TOKEN_KEYS__:
        val = env.get(key, "").strip()
        if val:
            return val
    raise RuntimeError(__MISSING_TOKEN_MSG_REPR__)


def identity_key(source: str, item_id: str) -> str:
    return f"{source.lower()}-{item_id.lower()}"


def idempotency_marker(
    source: str, item_id: str, rev: str, *, kind: str = "delivery"
) -> str:
    """Deterministic marker tag keyed identity+rev for find-before-create.

    Used as a PR branch suffix / comment tag / label body so a re-run of the
    same (identity, rev) finds the prior effect instead of creating a duplicate.
    """
    ident = identity_key(source, item_id)
    rev_digits = rev[1:] if rev.startswith("r") else rev
    safe = f"cairn-{kind}-{ident}-r{rev_digits}"
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in safe)


def decide_write_back(lookup_result: str) -> str:
    """Map a lookup outcome to an action: create | reuse | block.

    lookup_result in {"absent", "present", "uncertain"}.
    uncertain -> block (fail closed). Never create on doubt.
    """
    if lookup_result == "absent":
        return "create"
    if lookup_result == "present":
        return "reuse"
    if lookup_result == "uncertain":
        return "block"
    raise ValueError(f"unknown lookup_result {lookup_result!r}")


__EFFECTS_BODY__


def run_notify(
    work_item_path: Path,
    delivery_path: Path,
    *,
    find: Callable[..., str] | None = None,
    create: Callable[..., dict[str, Any]] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Find-before-create write-back. Returns exit code (9 = BLOCKED)."""
    doc = json.loads(work_item_path.read_text(encoding="utf-8"))
    item_id = str(doc.get("id") or "")
    rev = str(doc.get("rev") or "")
    source = str(doc.get("source") or SOURCE)
    if not item_id or not rev:
        print("work-item missing id/rev", file=sys.stderr)
        return EXIT_FAIL

    marker = idempotency_marker(source, item_id, rev)
    find = find or find_existing
    create = create or create_effect

    try:
        outcome = find(marker=marker, work_item=doc, environ=environ)
    except Exception as exc:
        print(f"notify lookup uncertain: {exc}", file=sys.stderr)
        return EXIT_BLOCKED

    if outcome not in ("absent", "present", "uncertain"):
        outcome = "uncertain"
    action = decide_write_back(outcome)
    if action == "block":
        print(
            f"notify BLOCKED: cannot confirm absence of marker {marker!r} — refuse to create",
            file=sys.stderr,
        )
        return EXIT_BLOCKED
    if action == "reuse":
        receipt = {
            "status": "reused",
            "marker": marker,
            "identity": identity_key(source, item_id),
            "rev": rev,
        }
        delivery_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        return EXIT_OK

    try:
        created = create(marker=marker, work_item=doc, environ=environ)
    except Exception as exc:
        print(f"notify create failed: {exc}", file=sys.stderr)
        return EXIT_FAIL
    receipt = {
        "status": "created",
        "marker": marker,
        "identity": identity_key(source, item_id),
        "rev": rev,
        **(created or {}),
    }
    delivery_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print(
            "usage: notify___PROVIDER__.py <work-item.json> <delivery.json>",
            file=sys.stderr,
        )
        return EXIT_FAIL
    return run_notify(Path(argv[0]), Path(argv[1]))


if __name__ == "__main__":
    raise SystemExit(main())
'''


_GITHUB_EFFECTS = r'''
# Bound hung provider calls; timeout -> UNCERTAIN (find) / failure (create).
GH_TIMEOUT_S = 30


def find_existing(
    *,
    marker: str,
    work_item: dict[str, Any],
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., str] | None = None,
) -> str:
    """Return absent|present|uncertain for the marker (issue comment tag).

    Lookup matches create_effect: issue comments only (no PR-branch path).
    Any API/transport/timeout error -> uncertain (fail closed; never create).
    """
    try:
        read_token(environ)
    except RuntimeError:
        return "uncertain"

    def _run(argv: list[str]) -> tuple[int, str, str]:
        if runner is not None:
            try:
                out = runner(argv)
            except Exception:
                return 1, "", "runner error"
            return 0, out, ""
        import subprocess
        try:
            res = subprocess.run(
                argv, capture_output=True, text=True, check=False, timeout=GH_TIMEOUT_S
            )
        except FileNotFoundError:
            return 127, "", "gh not found"
        except subprocess.TimeoutExpired:
            # Hung provider -> UNCERTAIN so run_notify exits BLOCKED(9).
            return 124, "", "gh timeout"
        return res.returncode, res.stdout, res.stderr

    # Issue comment containing the marker tag (matches create_effect).
    issue = str(work_item.get("id") or "")
    if not issue:
        return "uncertain"
    rc, out, _err = _run([
        "gh", "issue", "view", issue,
        "--comments",
        "--json", "comments",
    ])
    if rc != 0:
        return "uncertain"
    try:
        doc = json.loads(out) if out.strip() else {}
        comments = doc.get("comments") or []
    except json.JSONDecodeError:
        return "uncertain"
    for c in comments:
        body = (c.get("body") if isinstance(c, dict) else "") or ""
        if marker in body:
            return "present"
    return "absent"


def create_effect(
    *,
    marker: str,
    work_item: dict[str, Any],
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Create the delivery effect (issue comment tagged with the marker)."""
    read_token(environ)
    issue = str(work_item.get("id") or "")
    title = str(work_item.get("title") or "")
    rev = str(work_item.get("rev") or "")
    body = (
        f"<!-- {marker} -->\n"
        f"cairn delivery for `{identity_key(SOURCE, issue)}` rev `r{rev}`\n\n"
        f"Work item: {title}\n"
    )

    def _run(argv: list[str]) -> str:
        if runner is not None:
            return runner(argv)
        import subprocess
        try:
            res = subprocess.run(
                argv, capture_output=True, text=True, check=False, timeout=GH_TIMEOUT_S
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"gh timed out after {GH_TIMEOUT_S}s") from exc
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or res.stdout.strip() or "gh failed")
        return res.stdout

    _run(["gh", "issue", "comment", issue, "--body", body])
    return {"effect": "issue_comment", "issue": issue, "marker": marker}
'''


_SEAM_EFFECTS = r'''
def find_existing(
    *,
    marker: str,
    work_item: dict[str, Any],
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., str] | None = None,
) -> str:
    """Return absent|present|uncertain for the marker.

    # TODO: your __PROVIDER__ API call here — find-before-create keyed on marker.
    # On API error/timeout return "uncertain" (never raise into create).
    # __ENDPOINT_DOC__
    """
    try:
        read_token(environ)
    except RuntimeError:
        return "uncertain"
    _ = (marker, work_item, runner)
    # --- begin provider seam -------------------------------------------------
    # TODO: your __PROVIDER__ find-before-create lookup here
    return "uncertain"  # fail closed until the seam is implemented
    # --- end provider seam ---------------------------------------------------


def create_effect(
    *,
    marker: str,
    work_item: dict[str, Any],
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Create the delivery effect only after find_existing returned absent.

    # TODO: your __PROVIDER__ API call here
    """
    read_token(environ)
    _ = (marker, work_item, runner)
    raise NotImplementedError(
        "__PROVIDER__ notify create is a scaffold seam — implement create_effect()"
    )
'''


def _notify_script(spec: ProviderSpec) -> str:
    effects = _GITHUB_EFFECTS if spec.worked else _sub(_SEAM_EFFECTS, {
        "PROVIDER": spec.name,
        "ENDPOINT_DOC": spec.endpoint_doc,
    })
    return _sub(
        _NOTIFY_COMMON,
        {
            "PROVIDER": spec.name,
            "SOURCE_REPR": repr(spec.source),
            "PROVIDER_REPR": repr(spec.name),
            "TOKEN_DOC": _token_doc(spec),
            "TOKEN_KEYS": _token_keys_expr(spec),
            "MISSING_TOKEN_MSG_REPR": repr(_missing_token_msg(spec)),
            "EFFECTS_BODY": effects,
        },
    )


# --------------------------------------------------------------------------- #
# Config snippets (printed, never auto-edited)
# --------------------------------------------------------------------------- #


def _triggers_snippet(spec: ProviderSpec) -> str:
    return _sub(
        dedent(
            """\
            # --- paste into triggers.yaml (cairn new source __PROVIDER__; never auto-edited) ---
            fix-__PROVIDER__:
              pipeline: fix-__PROVIDER__
              watch: work/inbox/
              glob: "p*-__SOURCE__-*-r*.json"
              param: event
              identity: strict          # SG1 — generated source triggers DEFAULT strict
              lease: __LEASE__                 # SG4 — EXPLICIT serial-drain lease (not mtime grace)
              concurrency: 1
              order: aged
              inbox_max: __INBOX_MAX__             # spool cap the puller consults (list-only admit-wise)
              # waiting_max: 5
              # wip_max: 20
            # --- end triggers.yaml snippet ---
            """
        ),
        {
            "PROVIDER": spec.name,
            "SOURCE": spec.source,
            "LEASE": spec.lease,
            "INBOX_MAX": str(spec.inbox_max),
        },
    )


def _schedules_snippet(spec: ProviderSpec) -> str:
    return _sub(
        dedent(
            """\
            # --- paste into schedules.yaml (cairn new source __PROVIDER__; never auto-edited) ---
            pull-__PROVIDER__:
              cron: "__CRON__"
              run: [run, pull-__PROVIDER__, --headless, --idempotent]
            # --- end schedules.yaml snippet ---
            """
        ),
        {
            "PROVIDER": spec.name,
            "CRON": spec.poll_cron,
        },
    )
