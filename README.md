# cairn

**Artifact-native pipelines over coding-agent CLIs.**

> A cairn is a stack of stones travelers leave to mark a trail. Every step of a cairn pipeline
> leaves a validated artifact on disk; the trail of artifacts *is* the execution state. Resume
> means walking the trail to the last valid cairn. Nothing else remembers anything.

cairn is a small, declarative orchestrator for multi-phase agentic pipelines that delegate work to
coding-agent CLIs (Claude Code, Codex, Grok, …) as headless subprocesses — with typed artifacts as
the only interface between steps, validators as the only arbiter of done-ness, and the filesystem
as the only state.

## Status

**C0–C1 built and green (780 tests).** Implemented: the kernel (planner, walker, gatekit,
composer, artifacts, trail/runstate, guards, expression + template engines, config); all five
executors — `shell` and `stub` live, and the **`claude`, `codex`, and `grok` executors all
live-verified** (the first live `claude -p` / `codex exec` / `grok --prompt-file` runs, captured
as offline stub regressions in `tests/live/workspace-claude`, `tests/live/workspace-codex`, and
`tests/live/workspace-grok`), plus a **mixed fleet proven live** — one pipeline spanning
codex → claude → grok with per-step models recorded in `run.json`
(`tests/live/workspace-fleet`);
the workspace test layer (`cairn test` — validators/guards/pipelines/envelopes + `record`); the
full CLI — the `batch`/`learnings`/`gc`/`schedule` verbs are now **LIVE** (no longer stubs), and
first-class **scheduling has shipped** (`schedules.yaml`, cron/launchd/systemd installers,
content-key idempotency); and the `cairn new` scaffold. **v0.1.0 is tagged.** The hardening
backlog has since shipped too: opt-in `heartbeat` trail events, the webhook trail sink (tee, never
authority), kernel-side secret redaction, cross-version resume gates, range-scoped tool
enforcement at plan/run, batch failures that name their reason, and one aware-UTC clock behind
every persisted timestamp.
Every module went implement → review → fix. The day-0 pipeline runs end-to-end offline:
`uv run cairn run hello --headless`.

**Not done yet:** the CMS-population branch, the learnings curate/promote pipeline
(`self-improve.yaml`), and the brease-factory workspace migration (deferred;
it remains cairn's eventual first workspace — the C4/C5 pipeline-parity runs against a real
workspace are deferred with it). The doctor hook probe (`cairn doctor --probe-hooks`) now covers
all three vendor CLIs — on the dev machine Claude's, Codex's, and Grok's PreToolUse hooks all fire
and block headlessly (hook-primary; a per-machine, per-CLI-version fact). Design package in
[`docs/`](docs/): start with [`docs/README.md`](docs/README.md), then
[`docs/CONCEPTS.md`](docs/CONCEPTS.md), [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), and the
build order in [`docs/IMPLEMENTATION-PLAN.md`](docs/IMPLEMENTATION-PLAN.md) (C0–C7).

| Doc | What |
|---|---|
| [README](docs/README.md) | vision, philosophy, positioning vs LangGraph/Sandcastle/CI |
| [CONCEPTS](docs/CONCEPTS.md) | the noun/verb model — every moving part and its place |
| [ARCHITECTURE](docs/ARCHITECTURE.md) | kernel, execution semantics, guards, extension points |
| [API](docs/API.md) | file formats, template language, Executor protocol, CLI |
| [EXAMPLE-BREASE-REBUILD](docs/EXAMPLE-BREASE-REBUILD.md) | the real six-phase pipeline, fully expressed — the proof |
| [TOOLING-AND-GROWTH](docs/TOOLING-AND-GROWTH.md) | external tools, the maturation ladder, the learning loop |
| [IMPLEMENTATION-PLAN](docs/IMPLEMENTATION-PLAN.md) | C0–C7 build milestones, each independently verifiable |
| [DISTRIBUTION](docs/DISTRIBUTION.md) | packaging, versioning surfaces, scaffold, operator skill |
| [TESTING](docs/TESTING.md) | validation pyramid, stub executor, fixtures, envelope snapshots |
| [OBSERVABILITY](docs/OBSERVABILITY.md) | the Trail Protocol, sinks, OTel mapping, `cairn ps` |
| [SECURITY](docs/SECURITY.md) | secrets contract, prompt-injection posture, budgets |
| [SCHEDULING](docs/SCHEDULING.md) | first-class scheduling without a scheduler |

## Lineage — inspiration & aspiration

cairn was distilled from a working system, not invented in the abstract:

- **[brease-factory](../Brease/brease-factory/)** — a six-phase website-rebuild pipeline
  (capture → audit → blueprint → CMS → frontend → QA) built natively on Claude Code skills,
  subagents, hooks, and typed artifact gates. Every concept in cairn exists because that pipeline
  needed it; brease-factory is destined to become cairn's **first workspace**
  ([docs/EXAMPLE-BREASE-REBUILD.md](docs/EXAMPLE-BREASE-REBUILD.md) is that migration, specified).
- **[PORT-DESIGN.md](../Brease/brease-factory/docs/porting-research/PORT-DESIGN.md)** — the study
  that asked *"how do we run that pipeline on Codex and Grok too?"* and answered: an external
  driver calling each CLI headlessly once per phase, through a thin adapter, with artifacts as the
  interface. cairn is that answer promoted to a product — the driver became the kernel, the
  `CliAdapter` became the Executor protocol, and the port milestones became the implementation
  plan. The full research trail (CLI capability mappings, inventories) lives in
  [`brease-factory/docs/porting-research/`](../Brease/brease-factory/docs/porting-research/).

The aspiration, in one line: **the pipeline architecture that survived contact with reality, made
reusable — one orchestrator, any coding-agent CLI, every run a legible trail on disk.**
