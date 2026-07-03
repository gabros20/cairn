# cairn — Implementation Plan

The concrete build order: PORT-DESIGN §7's milestones re-based onto cairn. Same verification
discipline (every milestone independently checkable, smallest runnable slice first), same risks
(PORT-DESIGN §8.1 carries over unchanged — notably the Codex headless-hook probe), new framing:
the deliverable is the **framework + brease-factory as workspace #1**, not a one-off port.

**Ordering principle:** the kernel is fully verifiable with the `shell` executor alone — synthetic
pipelines, no LLM, no API keys, seconds in CI. So we prove the entire orchestration machine
deterministically (C0–C1) *before* any agent CLI enters (C2+). Model-driven milestones then only
ever debug one new thing at a time.

---

## Status 2026-07-03

- **C0 + C1 — complete.** Planner, walker, gatekit, composer, artifacts, trail/runstate, guards,
  expression + template engines, config, the `shell`/`stub` executors, the `cairn test` suite layer,
  the scaffold, and every C1-scope CLI verb are built and green (460 tests).
  *Deviation from the strict ordering:* built as parallel module waves with per-module
  implement→review→fix rather than strictly C0-then-C1. The C1 "synthetic-suite" verification bar is
  met by the 460-test suite + the offline `hello` end-to-end run + the testkit stub layer (a full
  pipeline replays offline through the `stub` executor).
- **C2 — partial.** Envelope composer and the `claude`/`codex`/`grok` executors are code-complete and
  unit-tested against fake binaries; the live `claude` parity run is still pending.
- **C4 — pending:** the Codex headless-hook (blocking-pretool) probe.
- **C5–C7 — not started:** Grok live setup, batch / CMS population / scheduling, package extraction
  (cairn to its own repo per DISTRIBUTION.md).

---

## C0 — Kernel skeleton + planner (no execution)

**Build:** `cairn/` package in this repo (`pyproject.toml`; runs as `uv run cairn`). `plan.py`
(load → resolve params/dims → expand conditionals → dataflow + reference verification), the
expression parser, `cairn plan` (+ `--json`), `cairn new workspace|pipeline|agent|skill|validator`.

**Verify:** the full `EXAMPLE-BREASE-REBUILD.md` pipeline (checked in as workspace files) plans
green in all three modes; seeded errors — typo'd artifact name, missing schema, `needs` with no
producer, unparseable expression — each yield a file+line diagnostic. Zero subprocesses spawned.

## C1 — Walker + shell executor (the whole machine, deterministically)

**Build:** `walk.py`, `artifacts.py` (globbing, schema+validator evaluation), `trail.py`,
run-dir bootstrap (`run.json` pinned schema), halt/resume, timeouts, per-step logs, `gatekit.py`
(TTY + `--gate` presets + `cairn gate` + exit 6), loop and parallel semantics, the `shell` **and
`stub` executors**, `cairn run/resume/validate/trail/doctor` (doctor: workspace lint only for now)
**+ `cairn test`** (validators/guards/pipelines suites — TESTING.md; the envelope suite lands with
`compose.py` in C2) **+ the Trail Protocol v1** (versioned envelope, seq offsets, `--follow --json
--since`, `gate-pending`/`heartbeat` events, `cairn ps` — OBSERVABILITY.md; webhook sink and OTel
exporter are post-C7 plugins) **+ the SECURITY.md kernel pieces** (run locking, `[secrets]`
declaration/doctor check, scrubbed-baseline env with per-agent pass-through; redaction and budgets
activate in C2/C3 when real executors produce output and usage).

**Verify — the synthetic suite (becomes permanent CI):** a fixture workspace whose steps are all
`run:` scripts exercising every semantic: all five node kinds; done-skip on resume; `kill -9`
mid-step then resume; gate answered by TTY, by `--gate`, by `cairn gate` after exit 6; loop exits
on `until`, caps with `on_cap: continue` and `halt`; parallel `wait_all` with one failing child;
validator failure → halt → reasons in trail → `retry.feedback` re-injects them; `{cycle}` paths;
timeout kill. **No model anywhere.** This suite is the framework's regression net forever.

## C2 — ClaudeExecutor + envelope + workspace #1 (P0→P2)

**Build:** `compose.py` (the six-block envelope, rendered to `logs/*.prompt.md`), the
`Executor` protocol + ClaudeExecutor (`claude -p`), tier resolution from `cairn.toml`, STEP
sentinel parsing, `[tools]` doctor checks. **Migrate:** skills to `skills/` at workspace root
(`.claude/skills` becomes a symlink; the thin wrapper skill stays), the P0–P2 agents to
`agents/*.yaml`, `validate-artifact.py` decomposed into per-artifact `validators/*.py`.

**Verify:** `cairn run brease-rebuild --param url=<test site> --to blueprint` on Claude produces
`captures/` + `decisions/` + `blueprints/` equivalent to a native-skill run (PORT-DESIGN M1's
parity check); the discovery gate fires mid-P0; the blueprint pair runs concurrently; envelope
files are complete and readable. **Then `cairn test record` that run** — the P0–P2 wiring becomes
a zero-token stub-run regression (+ envelope snapshots) from here on.

## C3 — Full pipeline on Claude, all three modes

**Build:** remaining agents/validators; the guard engine on Claude (hook + shim + post);
`escalate:` tier bumps; the art-review loop; qa + deploy steps; `manual:` brease-auth;
`learnings` trail events.

**Verify:** full `brease=off` builds in **rebuild** (baseline), **redesign** (escalation observed;
art-review runs ≥1 cycle; no-first-pass rule enforced by the validator), **reimagine** (strategy
fires; conditional chain completes). An injected F18 attempt is blocked at the hook layer and the
shim layer independently. At this point cairn replaces the native orchestrator path for this repo.

## C4 — CodexExecutor

**Build:** CodexExecutor (`codex exec`, `--output-schema` as bonus), `render_workspace` (AGENTS.md,
rules/permission bundle), tier table, **doctor's empirical hook probe** (PORT-DESIGN's top risk,
now a diagnosed per-machine fact; probe result selects hook-primary vs shim-primary guard posture).

**Verify:** P0 alone first, then `--to blueprint`, then full static pipeline; all three modes
(reimagine conditional chain included); guard demonstration under whichever posture the probe
selected; `redesign` escalates to the codex `reasoning` tier.

## C5 — GrokExecutor + mixed fleet

**Build:** GrokExecutor (`grok -p`), `setup-grok-config.sh` (BYOK effort-alias user config —
per-machine, like `brease login`), exit-2 guard hook branch, tier aliases.

**Verify:** parity with C4 on the same URL, three modes; `grok inspect`/doctor confirms skills +
hooks discovered; **mixed-fleet run** — build on Codex, `review` step pinned to Claude — completes
with per-step models recorded in `run.json`.

## C6 — Batch + CMS branch

**Build:** `cairn batch` (process pool of `cairn run --headless`); the `brease=on` branch
(brease-auth manual step, modeler, populate-approval gate with headless default **no**, populator,
wrong-CMS guard **fail-closed**); **scheduling** (`--idempotent`, `schedules.yaml`,
`cairn schedule install|list|run|uninstall` — SCHEDULING.md; it belongs here because it is thin
sugar over batch + headless + locking, all of which land in this milestone).

**Verify:** a 3-site batch per executor, gates preset, guards armed (per-process env — assert no
fail-open); one CMS build populates the correct env and a wrong-target mutation is blocked.

## C7 — Extraction + hardening

**Build:** extract `cairn/` to its own repo per the `DISTRIBUTION.md` spec (package anatomy §1,
compatibility surfaces §3, workspace scaffold §4 incl. the operator skill), tag `v0.1.0`,
`uv tool install git+…` path; workspace pins `requires`; CI = synthetic suite (C1) + `cairn plan`
over every workspace pipeline + doctor smoke. Optionally stub a 4th executor to prove nothing
leaked outside the plugin surfaces.

**Verify:** brease-factory runs against the *installed* cairn (not the in-repo copy); version-pin
mismatch is refused at plan time.

---

## Decision gates along the way

| At | Decision | Default |
|---|---|---|
| C0 start | confirm cairn replaces the straight port (this plan supersedes PORT-DESIGN M0–M7) | yes |
| C2 | workspace layout migration (skills to root, `.claude/` thins to wrapper+symlinks) | migrate |
| C4 | Codex guard posture — set by the doctor probe, not by judgment | probe decides |
| C7 | extraction timing — only when the Executor protocol has survived three real implementations | after C5 |

Risks: PORT-DESIGN §8.1 applies verbatim (Codex hooks/version churn, Grok user-config-only model
routing, undocumented Grok schemas, cross-vendor tier quality). One risk is *retired* by this
ordering: orchestrator-logic bugs can no longer be discovered mid-pipeline-run — C1's synthetic
suite catches them for free, forever.
