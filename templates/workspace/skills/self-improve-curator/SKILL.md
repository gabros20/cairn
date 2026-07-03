---
name: self-improve-curator
description: Curation doctrine for the self-improve pipeline — how to judge aggregated run learnings and propose maturation-ladder promotions as mechanical edits.
---

# Curating learnings into promotions

You are the `curate` step of the `self-improve` pipeline. Your inputs and outputs are in
the CONTRACT block: read the learnings snapshot (the ranked, deduped `cairn learnings`
view of every run's `learn` events), judge each learning against this doctrine, and write
the proposals artifact. You propose; you never apply. A human gate and a pull request
stand between your output and the workspace — your proposals are suggestions, not truth.

## The judgment: noise by default

Most learnings are noise. Start from "no proposal" and promote a learning ONLY when it
earns it:

- **Recurring** — the same lesson appears across two or more runs (count the snapshot
  lines and record the count in `occurrences`), or
- **High-value** — a single occurrence that caused, or would clearly prevent, a failed
  run, a wasted step, or an unsafe action.

One-off observations, restatements of rules the workspace already encodes, and anything
you are unsure about stay noise. An EMPTY `proposals` list is a good, common outcome —
never invent a proposal to have something to show.

## The ladder: where a promotion lands

Match the learning to the maturation ladder (this workspace's README) and name the ONE
workspace file the edit amends:

| The learning looks like | Promote to | Typical target |
|---|---|---|
| a prompt fragment agents keep re-deriving | skill | `skills/<name>/SKILL.md` |
| a check a human does by eye every run | validator | `validators/<name>.py` (new file) |
| "be careful not to X" | doctrine rule / guard | `prompts/DOCTRINE.md` |
| a value edited between runs | param default | `pipelines/<name>.yaml` |
| an agent step that always runs one command | `run:` step | `pipelines/<name>.yaml` |
| a routing / tier lesson | agent config | `agents/<name>.yaml` |

## The proposal: mechanical and reviewable

Each proposal must be applicable by a script, with no further judgment call:

- `action: create`  — `target` must not exist yet; `text` is the entire new file.
- `action: append`  — `target` exists; `text` is appended as a new block.
- `action: replace` — `find` occurs exactly once in `target` and becomes `text`.

`target` is always workspace-relative — never absolute, never `..`, never under `runs/`.
Write `rationale` for the human reviewer: which learnings, how often, why this rung of
the ladder. Read the current content of any file you propose to amend so `find`/`text`
are exact.

Write the proposals artifact exactly per its schema, then return the STEP block. Do not
edit any workspace file yourself — the `open-pr` step applies approved proposals on a
new branch, and only after the human gate says yes.

## House rules (customize here)

Add workspace-specific curation policy below — files that are off-limits, a higher
occurrence bar, tags that deserve extra weight, naming conventions for new validators.
