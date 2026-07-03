---
name: cairn-operator
description: How a coding agent (Claude Code, Codex, …) drives cairn as an operator — plan before run, run headless and watch the trail, relay gates and validator failures to the human, and never hand-edit a run dir. Load this whenever you are asked to run, resume, or babysit a cairn pipeline.
---

# Operating cairn

You are the **operator**: you drive `cairn` from the outside and relay between it and the human.
cairn does the orchestration; you translate its exit codes and JSON into decisions a person makes.
The whole integration between cairn and any conversational agent is this skill plus the CLI —
a CLI, exit codes, and JSON. Nothing else. Follow the six rules below exactly.

## The six rules

1. **Plan before run.** Always `cairn plan <pipeline> --json` first. It resolves params, expands
   conditionals, and static-verifies dataflow, schemas, agents, and skills — with zero tokens and
   zero side effects. Relay any diagnostics to the human verbatim. Never "fix" a plan error by
   editing `runs/`; fix the *workspace* (pipeline/agent/schema files), then re-plan.

2. **Run headless, watch the trail.** Start the run headless and in the background —
   `cairn run <pipeline> --headless …` — then poll `cairn trail <run-dir> --json` (or
   `--follow --json --since <seq>`) for status. Read progress from the **trail**, never by scraping
   the process's stdout: the trail is the single source of truth, resumable and ordered by `seq`.

3. **Exit 6 = a gate is waiting.** The run paused at a human decision point. Read the pending gate
   from the trail (its question, options, and the artifacts it summarizes), ask the human **through
   your own UI**, then record their answer with `cairn gate <run-dir> <name>=<choice>` and continue
   with `cairn resume <run-dir>`. Only relay the human's actual decision.

4. **Exit 3 = a validator failed.** An artifact did not meet its contract. Read `validator_reasons`
   from the trail, inspect the named artifact and `logs/<step>.log`, and propose a **workspace** fix
   (a corrected input, a loosened/tightened validator, a prompt change). Get the human's approval,
   apply it, then `cairn resume <run-dir>`. Do not paper over it by editing the artifact by hand.

5. **Exit 4/5 = machine trouble.** Something in the environment is wrong (executor auth, a missing
   tool, a version mismatch). Run `cairn doctor` (add `--executor X` to scope it), relay its
   findings and each printed fix to the human, and `cairn resume <run-dir>` once the machine is
   healthy.

6. **Never** hand-edit a run dir, answer a gate the human did not decide, or bypass a halt. Run dirs
   are the audit record and the resume source of truth; a run halts for a reason. Your job is to
   surface the halt and carry the human's decision back in — not to route around it.

## Why it is this thin

The boundary is deliberately a CLI + exit codes + JSON, so the "integration" is documentation, not
code. If you find yourself wanting to reach inside a run dir or improvise a gate answer, stop — that
is exactly the invariant these rules protect.
