# Getting started with cairn

A hold-your-hand path from nothing to a real, resumable pipeline run — offline, with zero API tokens.
By the end you will have installed cairn, scaffolded a workspace, run a pipeline, read its trail, and
understood how to point a step at a live coding-agent CLI.

This is the **tutorial**. It optimises for a first win, not for completeness — every command below was
run to produce the output you see. When you want the full reference, each step links out to it; don't
read those first.

> **What cairn is, in one breath.** A small declarative orchestrator for multi-phase pipelines that
> delegate each step to a coding-agent CLI (Claude Code, Codex, Grok) as a fresh headless process. The
> filesystem is the state machine: every step emits a typed artifact validated against a schema, an
> append-only trail records what happened, and resuming just walks the run directory to the last valid
> artifact. No database, no daemon, no session to lose.

---

## Before you start

You need two things on your machine:

- **Python ≥ 3.11**
- **[uv](https://docs.astral.sh/uv/)** — the Python packaging tool cairn runs under (`brew install uv`
  or `curl -LsSf https://astral.sh/uv/install.sh | sh`).

That's it. The `hello` pipeline you'll run needs no API keys, no model, and no network — it works the
moment the workspace exists.

---

## 1. Install cairn

Today cairn installs **from source**. Clone the repo and install it as a uv tool:

```console
$ git clone https://github.com/gabros20/cairn.git
$ cd cairn
$ uv tool install .
```

```console
Resolved 8 packages in 19ms
Installed 8 packages in 7ms
 + cairn==0.1.0 (from file:///…/cairn)
 + jsonschema==4.26.0
 + pyyaml==6.0.3
 …
Installed 1 executable: cairn
```

This puts a `cairn` executable on your PATH. Check it:

```console
$ cairn --version
cairn 0.1.0
```

**Prefer not to install globally?** Run it in place from the cloned repo instead — `uv sync` once, then
prefix every command with `uv run`:

```console
$ uv sync
$ uv run cairn --version
cairn 0.1.0
```

Everywhere below, `cairn …` and `uv run cairn …` are interchangeable; pick one.

> **After the public release**, the one-liner will be `uv tool install cairn-pipelines` (the PyPI
> distribution is named `cairn-pipelines`; the command and the Python import stay `cairn`). It is not on
> PyPI yet — use the from-source path above for now.

---

## 2. Scaffold a workspace

A **workspace** is a git repo holding your pipelines, agents, schemas, and validators. cairn ships a
starter:

```console
$ cairn new workspace demo
cairn: workspace 'demo' created at demo
  cd demo && cairn run hello
```

Here's what it created:

```
demo
├── cairn.toml                 # workspace config: executors, model tiers, defaults, tool preflight
├── pipelines
│   ├── hello.yaml             # the day-0 pipeline you'll run in step 3
│   └── self-improve.yaml      # the learning loop (aggregate → curate → gate → PR)
├── agents
│   ├── assistant.yaml         # a minimal agent, ready for your first agent step
│   └── curator.yaml           # the self-improve judge
├── schemas
│   ├── greeting.json          # JSON Schema contract for hello's greeting artifact
│   └── …
├── validators
│   ├── nonempty.py            # a validator: artifact exists + is non-empty
│   └── …
├── skills
│   ├── cairn-operator/        # how a coding agent drives cairn (ships in every workspace)
│   └── self-improve-curator/  # the curation doctrine you customise
├── scripts                    # deterministic helpers that run: steps call
│   └── self-improve-open-pr.py
├── prompts
│   └── DOCTRINE.md            # isolation + artifact-authority rules every agent inherits
├── tests
│   ├── matrix.yaml            # the offline test matrix for `cairn test`
│   ├── fixtures/              # artifact fixtures the validator tests check against
│   └── stubs/                 # recorded agent outputs the stub executor replays
├── allowlist.yaml             # named command fragments agents may run
├── .gitignore                 # ignores runs/ and local cruft
└── README.md                  # the workspace's own guide
```

Everything here is files — `git clone` reproduces the whole system. Move into it:

```console
$ cd demo
```

---

## 3. Your first run — zero tokens

The `hello` pipeline is built from **`run:` steps plus one gate** — deterministic shell commands, no
model, no auth — so it runs completely offline. Run it headless (headless = resolve any gate to its
default and never wait for a human):

```console
$ cairn run hello --headless
cairn: run complete → …/demo/runs/hello-world-20260703
```

A run directory appeared. It is the entire record of what happened:

```
runs/hello-world-20260703
├── run.json          # the run's identity: pipeline, params, cairn version, per-node status
├── trail.jsonl       # append-only event log (you'll read this in step 4)
├── greeting.json     # ← artifact produced by the `greet` step
├── message.txt       # ← artifact produced by the `compose` step
├── gates
│   └── tone.json     # the gate answer, written to disk like everything else
└── logs
    ├── greet.log     ·  greet.prompt.md
    └── compose.log   ·  compose.prompt.md
```

*(Abridged — omits internal bookkeeping: a `.cairn/` state dir and a `.cairn.lock` run lock.)*

The two artifacts are the actual output:

```console
$ cat runs/hello-world-20260703/greeting.json
{"name": "world", "pipeline": "hello"}

$ cat runs/hello-world-20260703/message.txt
Friendly hello, world!
```

And the gate — a decision point — recorded that no human answered, so it took its default:

```console
$ cat runs/hello-world-20260703/gates/tone.json
{"choice": "friendly", "by": "default", "at": "2026-07-03T22:28:11.101Z"}
```

> The run id is `hello-world-<date>`, where `<date>` is the run's UTC date — so you may see a different
> number than `20260703`. A same-day re-run gets a `-v2` suffix rather than overwriting.

---

## 4. Read the trail

`run.json` is the *current* state; `trail.jsonl` is the *history*. Read it with `cairn trail`:

```console
$ cairn trail runs/hello-world-20260703
   1  run-start        -                {"dims": {}, "executors": {"default": "claude"}, "params": {"name": "world"}}
   2  plan             -                {"models": {}, "nodes": ["greet", "tone", "compose"], "pipeline_hash": "sha256:8240…"}
   3  step-start       greet            {"log_path": "logs/greet.log", "model": "shell"}
   4  step-done        greet            {"artifacts": ["greeting.json"], "duration_s": 0.021}
   5  gate-answered    tone             {"by": "default", "choice": "friendly"}
   6  step-start       compose          {"log_path": "logs/compose.log", "model": "shell"}
   7  step-done        compose          {"artifacts": ["message.txt"], "duration_s": 0.020}
   8  run-done         -                {"nodes": 3}
```

The key idea in two sentences: **the artifacts *are* the state.** A step is "done" only if its outputs
exist and validate, so `cairn resume <run-dir>` doesn't replay a saved program counter — it walks the run
directory and re-runs from the first step whose artifact is missing or invalid. `kill -9` mid-run costs
you at most one step.

(`cairn trail --json` prints the raw JSONL — one event object per line — for piping into other tools.)

---

## 5. Anatomy of the `hello` pipeline

Open `pipelines/hello.yaml`. Stripped to its spine, it declares two artifacts and three steps:

```yaml
pipeline: hello
version: 1

params:
  name: { type: string, default: world }   # cairn run hello --param name=Ada

run_id: "hello-{params.name}-{date}"

artifacts:
  greeting:
    path: greeting.json                     # run-dir-relative
    schema: schemas/greeting.json           # ← validated as JSON Schema
  message:
    path: message.txt
    validator: validators/nonempty.py       # ← validated by a Python check

steps:
  - step: greet                             # a run: step — deterministic, no model
    run: >-
      python3 -c "import json, sys;
      json.dump({'name': sys.argv[1], 'pipeline': sys.argv[2]}, open(sys.argv[3], 'w'))"
      "{params.name}" "{pipeline}" "{artifact:greeting}"
    produces: [greeting]

  - gate: tone                              # a human decision → written to gates/tone.json
    reads: [greeting]
    ask: "What tone should the message use?"
    options: { friendly: "Warm and casual", formal: "Polished and professional" }
    default: friendly                       # headless runs resolve to this

  - step: compose
    run: >-
      python3 -c "…g['name']…"              # consumes {gate:tone} + {artifact:greeting}
    needs: [greeting, tone]
    produces: [message]
```

Four things to notice — they are the whole model:

- **step → artifact.** A step's job is to `produces:` one or more artifacts. Nothing is passed in memory;
  the next step reads the file.
- **artifact → schema/validator.** Every artifact names either a **schema** (JSON Schema) or a
  **validator** (a Python script that exits 0/1). The output is only accepted if it passes — that's the
  gate between steps.
- **executor.** These are `run:` steps, so they execute as plain shell — note `"model": "shell"` in the
  trail. `cairn.toml`'s `default_executor = "claude"` only kicks in for `agent:` steps (step 6). That's
  why `hello` needs no tokens: it has no agent steps yet.
- **placeholders.** `{params.name}` and `{pipeline}` inline *values*; `{artifact:greeting}` and
  `{gate:tone}` resolve to a *reference* — the artifact's absolute path, the gate's chosen option.

You can see the same shape without running anything — `cairn plan` is your typecheck. It statically
verifies params, dataflow, schemas, and validators, then prints the execution plan:

```console
$ cairn plan hello
hello (v1)
  params: name=world
  nodes:
    • greet [run]  → greeting
    ◆ gate tone [friendly/formal] default=friendly
    • compose [run]  needs greeting,tone  → message
```

As a flow, that's:

```mermaid
flowchart LR
  greet["greet · run"] -->|greeting.json| tone{{"gate: tone"}}
  tone -->|choice| compose["compose · run"]
  compose -->|message.txt| done([run-done])
```

Run `cairn plan hello` after every edit — when you add an agent step next, it also checks the agent, its
skills, and its artifacts all exist *before* any run spends a token. Full pipeline reference:
[API.md §2](API.md).

---

## 6. Wire a live executor

So far, no model. The moment a step needs judgment rather than a fixed command, you delegate it to a
coding-agent CLI. The scaffold has this queued up: the last, commented node in `hello.yaml` is an
**agent step**.

```yaml
  # - step: elaborate
  #   agent: assistant               # → agents/assistant.yaml (tier + skills + tools)
  #   args: { tone: "{gate:tone}" }  # exposed to the agent envelope as {{args.tone}}
  #   needs: [message]
  #   produces: [elaborated]
```

`agents/assistant.yaml` declares an abstract **tier**, not a vendor model:

```yaml
tier: balanced          # reasoning | balanced | cheap
effort: medium
skills: []
tools: { allow: [read, write], network: false }
```

`cairn.toml` maps that tier to a concrete model **per executor** — this is the only place vendor names
live:

```toml
[workspace]
default_executor = "claude"

[executors.claude.tiers]
reasoning = { model = "opus",   effort = "high" }
balanced  = { model = "sonnet", effort = "medium" }   # ← assistant (tier: balanced) resolves here
cheap     = { model = "haiku",  effort = "low" }
```

**Before you spend a token, preflight the machine** with `cairn doctor`. On a machine where the Claude
CLI is installed and authenticated, every line is a check:

```console
$ cairn doctor
✔ cairn 0.1.0
✔ workspace lint  2 pipelines plan green
✔ executor claude   healthy
✔ tool gh
✔ guard runner    cairn.kernel.guards imports
```

On a machine **without** that CLI (or not logged in), the executor line fails with the fix inline and
`doctor` exits non-zero (code 2), so CI catches it before a run does — the rest of the checks still
report:

```console
$ cairn doctor
✔ cairn 0.1.0
✔ workspace lint  2 pipelines plan green
✗ executor claude   executor 'claude' not found → install Claude Code and run `claude login`
✔ tool gh
✔ guard runner    cairn.kernel.guards imports
```

To actually enable the step: declare an `elaborated` artifact (with a schema or validator) under
`artifacts:`, uncomment the `elaborate` node, re-run `cairn plan hello` to typecheck it, and then:

```console
$ cairn run hello --param name=Ada          # now the elaborate step calls the claude executor
```

That run spends tokens, so it's left as *what you'd run* — the doctor output above is real; this line is
the one that goes live once you're ready. Executor reference and the full envelope an agent sees:
[API.md §3](API.md) and [API.md §6](API.md).

---

## 7. Test your workspace

Your workspace has its own test suite — separate from cairn's. `cairn test` runs the offline checks over
everything you've declared: validators against fixtures, guards against fixtures, and **full pipeline
walks through the stub executor** (agent steps replay recorded outputs, `run:` steps really run, gates
resolve to defaults) — all with zero tokens.

```console
$ cairn test
validators: 3 passed, 0 failed
guards: 0 passed, 0 failed
  note: (no fixtures)
pipelines: 2 passed, 0 failed
envelopes: 0 passed, 0 failed
  note: (no fixtures)
```

The two green pipeline walks are `hello` and `self-improve`, driven headlessly by `tests/matrix.yaml`.
This is how you verify wiring — dataflow, schemas, gate defaults — before any live run. Full story:
[TESTING.md](TESTING.md).

---

## Where to go next

You've now touched every core concept: pipeline, step, artifact, schema/validator, gate, executor, run,
and trail. From here:

- **Understand the model** — [CONCEPTS.md](CONCEPTS.md) (every moving part, why it exists) and
  [ARCHITECTURE.md](ARCHITECTURE.md) (how execution actually behaves: plan → walk → done → resume).
- **Look everything up** — [API.md](API.md): the complete `cairn.toml`, pipeline, and agent file
  formats, the expression grammar, the prompt envelope, and the full CLI reference.
- **See it at full scale** — [EXAMPLE-BREASE-REBUILD.md](EXAMPLE-BREASE-REBUILD.md): a real six-phase
  website-rebuild pipeline (parallel steps, a bounded review loop, gates) expressed in cairn.
- **Grow the workspace** — [TOOLING-AND-GROWTH.md](TOOLING-AND-GROWTH.md): how external tools enter a
  pipeline, the maturation ladder, and closing the learning loop with `cairn run self-improve`.
- **Run more than one thing** — `cairn batch` (one pipeline over a JSONL of params), `cairn schedule`
  (declared in `schedules.yaml`, installed into cron/launchd/systemd): [SCHEDULING.md](SCHEDULING.md),
  and `cairn trigger` (declared in `triggers.yaml`, fired by a file landing): [TRIGGERS.md](TRIGGERS.md).
- **Operate it in production** — [OBSERVABILITY.md](OBSERVABILITY.md) (the trail protocol, webhooks,
  `cairn ps`) and [SECURITY.md](SECURITY.md) (secrets, untrusted content, budgets, run locking).

The full docs map — sorted by what you're trying to do — is in [README.md](README.md).
