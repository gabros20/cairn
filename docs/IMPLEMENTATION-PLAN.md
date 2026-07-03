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
  the scaffold, and every C1-scope CLI verb are built and green (802 tests).
  *Deviation from the strict ordering:* built as parallel module waves with per-module
  implement→review→fix rather than strictly C0-then-C1. The C1 "synthetic-suite" verification bar is
  met by the suite + the offline `hello` end-to-end run + the testkit stub layer (a full
  pipeline replays offline through the `stub` executor).
- **C2 — complete (executor scope).** Envelope composer and the `claude`/`codex`/`grok` executors are
  code-complete, unit-tested against fake binaries, and **all three are now live-verified**. The
  claude live runs (captured as offline stub regressions in
  `tests/live/workspace-claude`) forced `--permission-mode bypassPermissions` and the `USER`/`LOGNAME`
  env baseline; the codex live runs (`tests/live/workspace-codex`) forced dropping `-a/--ask-for-approval`
  (gone from `codex exec` in codex-cli 0.142.5, which hardwires approval-never) and adding
  `--skip-git-repo-check`; the grok live runs (`tests/live/workspace-grok`, grok 0.2.82) forced
  `--output-format plain` (0.2.82 dropped `text`), `--prompt-file` delivery (headless stdin is dead —
  bare `-p` is an argv error), and `--permission-mode bypassPermissions` (`dontAsk` silently denies
  writes: exit 0, empty output, no artifact). The C2 pipeline-migration items still run against the
  deferred brease-factory workspace.
- **C6 verbs — shipped ahead of sequence.** `cairn batch` (process pool of `cairn run --headless`),
  `cairn learnings` (cross-run `learn`-event aggregation), `cairn gc` (dry-run retention, `--apply`
  to delete), and **first-class scheduling** (`schedules.yaml`, `cairn schedule install|list|run|
  uninstall`, cron/launchd/systemd backends, content-key idempotency) are all built and tested —
  LIVE, no longer stubs. The workspace **`requires`-pin** is enforced at plan time and cairn's
  version is **0.1.0**.
- **C4 — complete.** CodexExecutor is live-verified (above), and the doctor hook probe
  (`cairn doctor --probe-hooks`, `cairn/kernel/hookprobe.py`) has shipped. On the dev machine it
  returns **hook-primary** for both executors: `claude` PreToolUse fires+blocks under
  `bypassPermissions` — which **falsifies** ARCHITECTURE §4's open risk that `bypassPermissions` might
  disable hooks — and `codex` PreToolUse fires+blocks headless under `codex exec` (codex-cli 0.142.5
  *does* have native blocking hooks), so the "Codex guard posture" decision gate resolves to
  hook-primary. These are per-machine, per-CLI-version probe results, not universal guarantees. The
  C4 verify items that depend on brease-factory (P0 / blueprint-parity live runs) are **deferred with
  the workspace migration**; what C4 proves is the executor live-proof + the probe.
- **C5 — complete.** GrokExecutor is live-verified against grok 0.2.82 (`tests/live/workspace-grok`,
  model `grok-composer-2.5-fast`, recorded zero-token offline replay; argv facts under C2 above).
  *Plan deviation:* `setup-grok-config.sh` (the BYOK effort-alias user config) is **obsolete and was
  never built** — grok 0.2.82 ships a native headless `--effort low|medium|high|xhigh` flag matching
  cairn's effort enum exactly, so tier effort flows through as a flag like claude/codex, no alias
  config anywhere. The hook probe grew a grok recipe (`RECIPES["grok"]` in `cairn/kernel/hookprobe.py`)
  and on the dev machine verdicts **hook-primary**: grok PreToolUse fires+blocks under
  `bypassPermissions`. Grok's deny mechanism is `{"decision":"deny"}` on stdout — honored regardless
  of exit code — or exit 2; **any other hook failure (crash, timeout, malformed output) fails OPEN**.
  Live-only discoveries now encoded in code+tests: grok's shell tool is named `Shell`, not `Bash`
  (the recipe uses a catch-all matcher), and grok's claude/cursor compat cells load the user's
  `~/.claude` MCP servers into runs (the canary disables all compat cells). All three executors are
  now empirically hook-primary on the dev machine. **Mixed fleet proven** — the C5 verify bar:
  `tests/live/workspace-fleet` runs one pipeline, build (codex/gpt-5.5) → review (claude/haiku) →
  summarize (grok/grok-composer-2.5-fast), live green first try with per-step models recorded in
  `run.json` and cross-vendor dataflow asserted by schema; recorded offline replay + all four live
  workspaces planned and replayed in CI. The C5 parity items that need brease-factory (three-mode
  parity with C4) are **deferred with the workspace migration**, like C4's.
- **Backlog wave — shipped.** The post-C5 hardening backlog is built, two-stage-reviewed, and
  green: opt-in `heartbeat` trail events (`[defaults] heartbeat`; off by default; no beat after a
  step's terminal event — OBSERVABILITY §1); `usage` plumbed end-to-end (`Result.usage`; executors
  pass `None` today, the schema is the deliverable); the webhook trail sink (`[sinks.webhook]` tee
  — jsonl stays the byte-identical synchronous authority — OBSERVABILITY §2); kernel-side secret
  redaction live per SECURITY §1.3; batch failures naming their reason (bounded stderr tail in
  `RunOutcome.error` + the batch summary); cross-version resume gates per DISTRIBUTION §3 + a
  doctor `requires` line; range-scoped tool enforcement at plan (warn) and run/resume (hard-stop
  before minting) per TOOLING §2; and one aware-UTC clock behind every persisted timestamp
  (`{date}` buckets by UTC day). The `v0.1.0` tag is cut. Internal seam unification
  (batchkit/schedkit over one `proc.SubprocessRunner`) rode along. Learnings curate/promote was
  deliberately held out of this wave — it has since shipped (next bullet).
- **Self-improve — shipped, as scaffold furniture.** The learning loop's `curate`/`promote` rows
  are built per the placement ruling (user-approved): the framework ships the **mechanism**, the
  workspace owns the **policy** — NOT a kernel verb, NOT a vendor skill. In the
  `cairn new workspace` scaffold: `pipelines/self-improve.yaml` (aggregate `run:` — `cairn
  learnings --since/--tag` → typed snapshot → curate agent step → schema+validator-checked
  `proposals.json` → approve human gate with headless default **no** — a cron run cannot
  self-promote, proven by the scaffold's own recorded test → open-pr `run:` script: temporary git
  worktree so the working branch is structurally untouchable, applies only approved+re-validated
  workspace-relative targets, branch `self-improve/<run-id>`, PR via `gh`, exit 1 rather than an
  empty PR, branch kept on push failure for retry); `agents/curator.yaml` + the vendor-free
  curation-doctrine skill (`skills/self-improve-curator/SKILL.md`); and `[tools.gh]
  needed_by=["open-pr"]`, dogfooding tool enforcement. Retrofittable into an existing workspace
  via `cairn new pipeline self-improve` (append-only, wires the test-matrix row, never clobbers
  customized companions). Hard rules preserved: PRs only — suggestions, not truth; curation never
  automatic; the human gate mandatory (TOOLING §7). Suite 780 → 802.
- **Still ahead:** the brease-factory workspace migration — workspace #1, whose scope includes its
  `brease=on` CMS-population branch (Brease is separate tooling, not a framework feature; see C6
  note + EXAMPLE-BREASE-REBUILD) — plus the C2–C5 parity runs deferred with it.

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
--since`, `gate-pending`/`heartbeat` events, `cairn ps` — OBSERVABILITY.md; the webhook sink has
since shipped, the OTel exporter stays a post-C7 plugin) **+ the SECURITY.md kernel pieces** (run
locking, `[secrets]` declaration/doctor check, scrubbed-baseline env with per-agent pass-through;
redaction has since shipped — see Status; budgets stay future until executors report usage).

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

## C4 — CodexExecutor  *(complete — see Status)*

**Build:** CodexExecutor (`codex exec`, `--output-schema` as bonus), `render_workspace` (AGENTS.md,
rules/permission bundle), tier table, **doctor's empirical hook probe** (PORT-DESIGN's top risk,
now a diagnosed per-machine fact; probe result selects hook-primary vs shim-primary guard posture).
*Shipped:* the executor is live-verified (`tests/live/workspace-codex`), and the probe
(`cairn doctor --probe-hooks`, `cairn/kernel/hookprobe.py`) returns **hook-primary** for both claude
and codex on the dev machine (grok joined at C5 — all three now probe hook-primary).

**Verify:** P0 alone first, then `--to blueprint`, then full static pipeline; all three modes
(reimagine conditional chain included); guard demonstration under whichever posture the probe
selected; `redesign` escalates to the codex `reasoning` tier. *The pipeline-level verify items
(P0 / blueprint-parity live runs) run against a real workspace and are deferred with the
brease-factory migration; the executor live-proof + the hook probe are done.*

## C5 — GrokExecutor + mixed fleet  *(complete — see Status)*

**Build:** GrokExecutor — as shipped against grok 0.2.82: `--prompt-file` (headless stdin is dead),
`--output-format plain`, `--permission-mode bypassPermissions`, and the **native `--effort` flag**
— plus the grok hook-probe recipe (`RECIPES["grok"]`). *Plan deviations:* this section originally
called for `setup-grok-config.sh` (a BYOK effort-alias user config — per-machine, like `brease
login`) and an "exit-2 guard hook branch"; the alias config is **obsolete, never built** (0.2.82's
native `--effort low|medium|high|xhigh` covers cairn's effort enum exactly), and the exit-2 branch
became the probe recipe's belt-and-braces deny — grok honors `{"decision":"deny"}` on stdout
regardless of exit code, and exit 2 alone also denies (everything else fails open).

**Verify:** *the mixed-fleet bar is met exactly as specified* — build on Codex, `review` step
pinned to Claude (plus grok as a third leg: summarize) completes with per-step models recorded in
`run.json` (`tests/live/workspace-fleet`, live green + offline replay in CI); doctor's probe
confirms grok's hooks fire+block (hook-primary, dev machine). The three-mode parity runs against
the brease-rebuild workspace are deferred with the brease-factory migration (as with C4).

## C6 — Batch + CMS branch

*Status: batch, learnings, gc, and scheduling below are **built and tested (LIVE)** — shipped ahead
of sequence. Scope note (user ruling): the `brease=on` CMS-population branch is not a framework
milestone — Brease is separate tooling, and the branch belongs to the brease-factory **workspace
migration** (workspace #1, specified in EXAMPLE-BREASE-REBUILD). The build/verify text below stays
as the historical milestone description.*

**Build:** `cairn batch` (process pool of `cairn run --headless`); the `brease=on` branch
(brease-auth manual step, modeler, populate-approval gate with headless default **no**, populator,
wrong-CMS guard **fail-closed**); **scheduling** (`--idempotent`, `schedules.yaml`,
`cairn schedule install|list|run|uninstall` — SCHEDULING.md; it belongs here because it is thin
sugar over batch + headless + locking, all of which land in this milestone).

**Verify:** a 3-site batch per executor, gates preset, guards armed (per-process env — assert no
fail-open); one CMS build populates the correct env and a wrong-target mutation is blocked.

## C7 — Packaging + hardening

*cairn is already its own standalone repo (DISTRIBUTION §2), so "extraction" here is **packaging**,
not a repo move. The workspace `requires`-pin refusal at plan time has already landed (see Status).*

**Build:** package this repo per the `DISTRIBUTION.md` spec (package anatomy §1, compatibility
surfaces §3, workspace scaffold §4 incl. the operator skill), tag `v0.1.0`, `uv tool install git+…`
path; CI = synthetic suite (C1) + `cairn plan` over every workspace pipeline + doctor smoke.
Optionally stub a 4th executor to prove nothing leaked outside the plugin surfaces.

**Verify:** brease-factory runs against the *installed* cairn (not the run-in-place copy); version-pin
mismatch is refused at plan time (**done** — `cairn plan` via `config.check_requires`).
*Status: the `v0.1.0` tag is cut; the installed-run verify waits on the brease-factory migration.*

---

## Decision gates along the way

| At | Decision | Default |
|---|---|---|
| C0 start | confirm cairn replaces the straight port (this plan supersedes PORT-DESIGN M0–M7) | yes |
| C2 | workspace layout migration (skills to root, `.claude/` thins to wrapper+symlinks) | migrate |
| C4 | Codex guard posture — set by the doctor probe, not by judgment | **resolved: hook-primary** (probe, dev machine) |
| C7 | packaging/tag timing — only when the Executor protocol has survived three real implementations | after C5 *(satisfied — C5 done, three vendor executors live; `v0.1.0` tagged)* |

Risks: PORT-DESIGN §8.1 applies verbatim (Codex hooks/version churn, Grok user-config-only model
routing, undocumented Grok schemas, cross-vendor tier quality). Two are *retired*: orchestrator-logic
bugs can no longer be discovered mid-pipeline-run — C1's synthetic suite catches them for free,
forever — and Grok's user-config-only model routing (grok 0.2.82 takes `-m` plus a native `--effort`
flag directly; no user-config aliasing needed).
