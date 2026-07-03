# cairn — artifact-native pipelines over coding-agent CLIs

> **A cairn is a stack of stones travelers leave to mark a trail.** Every step of a cairn pipeline
> leaves a validated artifact on disk; the trail of artifacts *is* the execution state. Resume means
> walking the trail to the last valid cairn. Nothing else remembers anything.

`cairn` is the framework distilled from the brease-factory pipeline and its port design
(`docs/porting-research/PORT-DESIGN.md`): a small, declarative orchestrator for **multi-phase agentic
pipelines that delegate work to coding-agent CLIs** (Claude Code, Codex, Grok, …) as headless
subprocesses, with **typed artifacts as the only interface between steps**.

It is what we would build *instead of* porting brease-factory three times — and what we would
migrate the existing Claude Code implementation onto. The PORT-DESIGN's driver + `CliAdapter` is the
embryo of this framework; cairn is that seam generalized, named, and given a real DX.

---

## Why this exists (and why not LangGraph / Sandcastle / CI)

We evaluated the alternatives seriously (see the conversation record + PORT-DESIGN §8):

| | Orchestrates | State model | Agents are | Verdict for our problem |
|---|---|---|---|---|
| **LangGraph** | in-process LLM graphs | checkpointer DB (competes with disk) | SDK calls sharing message state | Wrong layer. Its checkpointer and our artifacts would be two authorities on "where is this run?" — a bug class our design structurally lacks. |
| **Sandcastle** | agent CLI subprocesses | git branches merged back | code-editing sessions | Right layer, wrong center of gravity: built for *edit-code-and-merge*; our agents are *artifact generators in run dirs*. We'd use it with its core feature off. |
| **CI runners** (GH Actions…) | shell jobs | opaque per-job workspaces | not a concept | Right skeleton (declarative steps, artifacts) but no agent envelope, no gates-as-data, no validation edges, cloud-shaped. |
| **Claude Code native** (today) | skills + Workflow JS | artifacts (ours) + session | subagents (one vendor) | What we're generalizing away from: the orchestration is welded to one CLI's primitives. |
| **cairn** | agent CLI subprocesses | **the filesystem, validated** | fresh headless processes | Every concept below exists because the brease pipeline needed it; nothing else got in. |

The one good idea in graph frameworks — *topology as data* — we keep. Everything else they sell
(checkpointers, interrupts, streaming state) our filesystem already does better for this class of
system.

## The philosophy (inherited, now enforced by a framework)

1. **The filesystem is the state machine.** All run state = files in one run directory. `run.json`
   (pinned schema), `trail.jsonl` (event log), artifacts, gate decisions, logs, rendered prompts.
   There is no database, no checkpointer, no in-memory session to lose. `kill -9` at any moment
   loses at most one step's work.
2. **Agents are processes.** Every delegation is one fresh headless CLI invocation (`claude -p`,
   `codex exec`, `grok -p`). Full context isolation is a property of the OS, not a framework promise.
   The CLI is a swappable **executor** — pipelines don't know which one is running, and different
   steps of one run may use different executors ("mixed fleet").
3. **Contracts over conversation.** A step's interface is `needs` (input artifacts) → `produces`
   (output artifacts, schema-validated) + a typed return summary. Transcripts are never parsed for
   state. If it matters, it's in a file with a schema.
4. **Determinism is enforced, not trusted.** Validators gate every edge; a step is *done* if and only
   if its outputs validate. Guards wrap dangerous commands with defense-in-depth (native hook + PATH
   shim + post-hoc validation). The orchestrator is deterministic code; only the *inside* of a step
   is model-driven.
5. **Humans are steps too.** Gates (decisions) and manual steps (do-this-by-hand) are first-class
   nodes owned by the orchestrator. Gate answers are written to disk as artifacts — replayable,
   auditable, and resumable like everything else.
6. **Small core, declarative surface.** Pipelines, agents, artifacts, gates, guards, tiers: YAML.
   Skills: markdown. Only validators, guards, and executors are code. The kernel is a small,
   dependency-light body of Python (stdlib + `pyyaml` + `jsonschema`, no other runtime deps) —
   ~5.7k lines as built; everything beyond it is a plugin.
7. **AX is a design surface.** The *agent experience* — what a model sees when invoked — is a
   deterministic, auditable envelope (mission → contract → skills → trail context → doctrine →
   return protocol), rendered to a file before execution. No hidden context, no auto-magic loading,
   absolute paths always. See `ARCHITECTURE.md §6`.

## What it looks like

```yaml
# pipelines/brease-rebuild.yaml (excerpt — full version in EXAMPLE-BREASE-REBUILD.md)
steps:
  - id: discover
    agent: site-extractor
    produces: [discovery]

  - gate: scope                      # human decision, owned by the orchestrator
    when: params.pages == 'gate'
    reads: [discovery]
    default: all                     # headless runs resolve from defaults

  - id: capture
    agent: site-extractor
    needs: [discovery, scope]
    produces: [site-map, design-signals]

  - parallel: blueprint              # concurrent pair, disjoint outputs
    steps:
      - { id: architect,     agent: blueprint-architect, needs: [mode-plan], produces: [blueprints] }
      - { id: design-author, agent: design-director,     needs: [mode-plan], produces: [design-md] }

  - loop: art-review                 # bounded review⇄revise cycle
    min: 1
    max: { interactive: 3, headless: 2 }
    until: artifacts.art-review.verdict == 'approve'
    body:
      - { id: review, agent: design-director, produces: [art-review] }
      - { id: revise, agent: frontend-builder, unless: artifacts.art-review.verdict == 'approve' }
```

```console
$ cairn run brease-rebuild --param url=https://acme.com --param mode=redesign --executor codex
$ cairn run brease-rebuild ... --executor grok --step-executor review=claude   # mixed fleet
$ cairn resume runs/acme-redesign-20260702        # walks the trail, re-runs first invalid step
$ cairn plan brease-rebuild --param mode=reimagine # static verify + printed execution plan, no run
```

## The concept map (each part's one place)

| Concept | Is | Lives in | Full spec |
|---|---|---|---|
| **Pipeline** | declarative trail of steps | `pipelines/*.yaml` | `API.md §2` |
| **Step** | one delegation: agent / script / manual | pipeline file | `API.md §2.3` |
| **Artifact** | typed file contract (path + schema + validator) | pipeline `artifacts:` | `API.md §2.2` |
| **Agent** | worker declaration: tier, effort, skills, tools | `agents/*.yaml` | `API.md §3` |
| **Executor** | CLI binding (claude/codex/grok/shell) | plugin + `cairn.toml` | `API.md §6` |
| **Skill** | markdown capability pack | `skills/<name>/SKILL.md` | `CONCEPTS.md §7` |
| **Gate** | human decision point → decision artifact | pipeline step | `API.md §2.4` |
| **Guard** | pre-execution command policy | `guards:` + `guards/*.py` | `API.md §5` |
| **Validator** | pure check: artifact → pass/fail + reasons | `validators/*.py` | `API.md §4` |
| **Run** | one execution = one directory | `runs/<id>/` | `API.md §8` |
| **Trail** | append-only event log of a run | `runs/<id>/trail.jsonl` | `API.md §8.2` |

## Documents in this folder

- **[CONCEPTS.md](CONCEPTS.md)** — the noun/verb model: every moving part, why it exists, what
  breaks without it.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — kernel layout, execution semantics (plan → walk → done →
  resume → halt), loop/parallel/gate semantics, guard enforcement matrix, batch, reproducibility,
  extension points.
- **[API.md](API.md)** — the complete file-format and code reference: `cairn.toml`, pipeline schema,
  agents schema, expression grammar, the prompt envelope (AX spec), the STEP return protocol, the
  `Executor` protocol, CLI reference, run layout + trail event schema.
- **[EXAMPLE-BREASE-REBUILD.md](EXAMPLE-BREASE-REBUILD.md)** — the entire brease-rebuild pipeline
  (all three modes, gates, the P4.5 loop, CMS branch, deploy) expressed in cairn — the proof the
  abstraction covers the real system.
- **[TOOLING-AND-GROWTH.md](TOOLING-AND-GROWTH.md)** — how external tools (crawl4ai, vercel, gh,
  brease…) enter a pipeline (verify / teach / permit / use), the workspace maturation ladder, and
  authoring workspaces with a coding agent (`cairn plan` as the agent's typecheck).
- **[IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md)** — the build order (C0–C7): kernel proven
  deterministically first (synthetic no-LLM suite), then Claude → Codex → Grok, batch + CMS,
  extraction. Supersedes PORT-DESIGN §7's M0–M7 once cairn is confirmed as the target.
- **[DISTRIBUTION.md](DISTRIBUTION.md)** — the mechanics behind §Packaging & embedding: package
  anatomy + entry points, the three compatibility surfaces (pipeline schema / executor protocol /
  run-dir format), the workspace scaffold spec, `cairn doctor` onboarding, and the operator skill
  that integrates cairn with any coding agent.
- **[TESTING.md](TESTING.md)** — the five-layer validation pyramid; the workspace test layer
  (`cairn test`): validator/guard fixtures (killing false-green), **stub-executor pipeline runs**
  (full wiring verified offline through production code, zero tokens), envelope snapshots, and
  `cairn test record` (harvest a real run into regression fixtures).
- **[OBSERVABILITY.md](OBSERVABILITY.md)** — the Trail Protocol: the append-only event log
  formalized (versioned envelope, monotonic `seq` offsets, single-writer guarantees), three
  consumption tiers (follow the file / webhook sinks / OTel export mapping), `gate-pending` +
  `heartbeat` events, and `cairn ps` for the cross-run fleet view. Lineage: Bazel BEP, Kafka,
  Nextflow weblog — no servers, no DBs.
- **[SECURITY.md](SECURITY.md)** — containment over trust: the secrets contract (names declared,
  values env-only, per-agent pass-through deny-by-default, kernel-side redaction), the
  prompt-injection posture for scraped content (trust tiers + the cage: allowlist/guard/gate/
  isolation), network declarations, budgets (exit 7), run locking, and `cairn gc` retention.
- **[SCHEDULING.md](SCHEDULING.md)** — first-class scheduling without a scheduler: `schedules.yaml`
  declared in the workspace, installed into the host scheduler, fired as idempotent invocations;
  unattended safety = headless gate defaults + budgets + locks + webhook notification.

## Relation to PORT-DESIGN.md

PORT-DESIGN answered *"how do we run the existing pipeline on three CLIs?"* — its answer (external
driver + `CliAdapter` + portable core) is correct and **cairn is that answer promoted to a product**:

- PORT-DESIGN `driver/brease_run.py` → the cairn **kernel walker**
- PORT-DESIGN `CliAdapter` (5 ops) → the cairn **Executor protocol** (same five responsibilities)
- PORT-DESIGN `core/pipeline.yaml` + `agents.yaml` → cairn's **pipeline + agent files** (generalized)
- PORT-DESIGN §3.3 control-flow shapes → cairn's **five node kinds** (step, gate, parallel, loop, manual)
- PORT-DESIGN §4 enforcement → cairn's **guard engine** with the same defense-in-depth

Adopting cairn changes the port plan's framing, not its milestones: M0/M1 *become* "build the cairn
kernel + ClaudeExecutor and express brease-rebuild as a cairn workspace"; M2–M5 become "add
CodexExecutor/GrokExecutor". Same order, same verifications, same risks (the Codex headless-hook
test is unchanged) — but the deliverable is a reusable framework plus brease-factory as its first
workspace, instead of a one-off port.

## What we knowingly give up

Honesty section. Migrating off Claude-Code-native orchestration costs:

- **The interactive session UX** — a native `/brease-rebuild` conversation with inline
  AskUserQuestion is nicer than a TTY driver for single-site exploratory runs. *Mitigation:* keep a
  thin Claude skill that shells out to `cairn run` and relays gates; the loss is small because every
  long run is mostly unattended anyway.
- **Native subagent ergonomics** — Claude's Agent tool gives in-session spawn with zero process
  management. cairn re-buys this with OS processes, which is more fidelity but more moving parts
  (timeouts, logs, zombie cleanup — all kernel-owned).
- **Anthropic's Workflow JS engine** for ad-hoc fan-outs. cairn pipelines are declared, not scripted;
  truly ad-hoc orchestration stays in whatever CLI you're chatting with.

We judge all three acceptable; none touches the pipeline's correctness properties.

## Packaging & embedding

cairn is a **build tool** and distributes like one (dbt / terraform / make): three layers, one
answer each — never a vendored kernel copy per project. (This section is the philosophy; the
operational mechanics — package anatomy, versioning surfaces, scaffold, onboarding, the operator
skill — are specified in [DISTRIBUTION.md](DISTRIBUTION.md).)

| Layer | What | Distributed as |
|---|---|---|
| **Tool** | the `cairn` CLI (kernel + built-in executors) | versioned Python package — incubated in-repo (`cairn/` + pyproject, `uv run cairn`), extracted to its own repo at API stability (`uv tool install git+…@v0.1.0`), PyPI only if open-sourced |
| **Workspace** | pipelines/agents/skills/validators + `cairn.toml` | a git repo (brease-factory is workspace #1); starter via `cairn new workspace` (a template repo is optional sugar over that) |
| **Runs** | `runs/<id>/` | gitignored artifacts, never distributed |

The workspace pins its tool (`cairn.toml: requires = ">=0.1,<0.2"`, checked at plan time);
`run.json` records the exact version per run.

**Four run postures, one binary:**
1. **Terminal** (primary) — foreground process, TTY gates; `--headless` for CI/batch. No
   daemon: when no run is active, cairn doesn't exist.
2. **Operated by a coding agent** — the agent drives cairn through Bash like any build tool. Gates
   resolve via the **operator pattern**: an unanswered gate exits code 6 → the agent reads
   `cairn trail --json`, asks the human through its *own* UI, answers with
   `cairn gate <run-dir> <name>=<choice>`, then `cairn resume`. This embeds cairn in any
   conversational agent with zero integration code — the thin `/brease-rebuild` wrapper skill is
   ~20 lines of run/watch/relay/resume.
3. **Agents inside cairn** — the executors. The symmetry is intentional: the same CLI can operate a
   run from above and execute steps within it; every boundary is a process + artifacts.
4. **Scheduled** — declared in `schedules.yaml`, installed into the *host* scheduler
   (`cairn schedule install` → cron/launchd/systemd), fired as idempotent invocations
   (`--idempotent`: re-fire = resume-or-no-op, failure catch-up = the next firing resumes). cairn
   owns schedulability, never the clock — no daemon ([SCHEDULING.md](SCHEDULING.md)).

Ruled out: vendored kernels (the drift disease, one level up), `curl|bash` installers (uv exists),
MCP-server-first (a later plugin at most — the operator pattern already works everywhere), and any
resident daemon (the filesystem is the state).

## Status

**C0–C1 built and green (460 tests).** Implemented: the kernel (planner, walker, gatekit, composer,
artifacts, trail/runstate, guards, expression + template engines, config, doctor, scaffold); all
five executors (`shell`/`stub` live; `claude`/`codex`/`grok` code-complete and unit-tested against
fake binaries, **not yet live-verified**); the workspace test layer (`cairn test` + `record`); and
the full C1-scope CLI (`batch`/`learnings`/`gc`/`schedule` still stubbed → exit 2). The day-0
pipeline runs end-to-end offline (`cairn run hello --headless`). Still ahead, per
[IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md) (C0–C7): live-model parity (C2/C3), the Codex
headless-hook probe (C4), Grok live setup (C5), batch / CMS population / scheduling (C6), and
package extraction to its own repo (C7).
