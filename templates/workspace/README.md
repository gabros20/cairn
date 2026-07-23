# {{WORKSPACE_NAME}}

A [cairn](https://github.com/designatives/cairn) workspace: pipelines, agents, skills, schemas, and
validators that a coding-agent CLI executes step by step, validating a typed artifact between each.
Everything here is files — `git clone` reproduces the whole system.

## Day 0 — it runs offline, right now

The `hello` pipeline needs no auth, no API keys, and no model: it is built from `run:` steps plus one
gate, so it works the moment this workspace exists.

```console
$ cairn plan hello                 # static-verify: params, dataflow, schemas, validators — zero tokens
$ cairn run hello                  # writes runs/hello-world-<date>/  (greeting.json + message.txt)
$ cairn run hello --param name=Ada # override a param
$ cairn trail runs/hello-<...>     # replay what happened, step by step
```

`cairn plan` is your typecheck — run it after every edit. When you add an agent step later, it also
checks that the agent, its skills, and its artifacts all exist before anything runs.

## Firing from an event instead of typing the command

Nothing ships by default — `cairn run hello` above is you, at the keyboard. When a pipeline should
run because a file landed (a script's output, a webhook bridge's payload) rather than because you
typed a command, add a `triggers.yaml` at the workspace root and sync it into the host watcher:

```yaml
# triggers.yaml (none shipped by default — uncomment and adapt)
# handle-reply:
#   pipeline: hello
#   watch: inbox/replies/          # workspace-relative; one run per new file
#   param: name                    # hello's only declared param — the claimed file's path
#                                   # becomes the greeting name (quirky, but runnable as-is);
#                                   # point a real pipeline's param at the payload instead
#   # optional W3 back-pressure (absent = serial unbounded drain, today's default):
#   # concurrency: 1               # max children at once (>1 = bounded pool)
#   # order: name                  # "name" | "aged" (priority aging by mtime)
#   # waiting_max: 5               # stop admitting when needs-human depth is full
#   # blocked_max: 5               # default = waiting_max when set
#   # capacity_max: 10             # stop on capacity-park depth
#   # wip_max: 20                  # claimed + all waiting
#   # inbox_max: 50                # spool cap for pullers (W4); list-only until then
# cairn trigger sync --backend launchd   # or systemd; cron cannot host a file watch
```

Full reference — the claim/consume at-most-once semantics, the `cursor:` poll-source primitive for
providers that only answer polls, and the webhook-bridge pattern — is `docs/TRIGGERS.md` in the cairn
repo (`docs/SCHEDULING.md` for the clock-driven sibling, `schedules.yaml`).

## What's here

| Path | What it is |
|---|---|
| `cairn.toml` | workspace config — executors, model tiers, defaults, `[tools]` preflight |
| `pipelines/hello.yaml` | the day-0 pipeline; grow it by uncommenting the `agent:` step |
| `pipelines/self-improve.yaml` | the learning loop: aggregate → curate → approve (gate) → PR |
| `agents/assistant.yaml` | a minimal agent, ready for that first agent step |
| `agents/curator.yaml` · `skills/self-improve-curator/` | the self-improve judge + its curation doctrine (customize the skill) |
| `scripts/` | deterministic helpers `run:` steps call (e.g. self-improve's open-pr) |
| `tests/` | `cairn test` furniture: artifact fixtures, stub replays, the pipeline matrix |
| `skills/cairn-operator/` | how a coding agent drives cairn as an operator (ships in every workspace) |
| `schemas/` · `validators/` | artifact contracts (JSON Schema) and acceptance checks |
| `prompts/DOCTRINE.md` | the isolation + artifact-authority invariants every agent inherits |
| `allowlist.yaml` | named command fragments agents may run |
| `runs/` | every execution, self-describing (gitignored) |

## Growing the workspace — the maturation ladder

Start sparse. Let a real run teach you the next promotion; the framework never punishes sparseness —
validators, guards, gates, and skills are all *additive* declarations.

| Signal from real runs | Promotion |
|---|---|
| the same prompt fragment recurring across steps | → **skill** |
| you check an output by eye every run | → **validator** (encode the check) |
| "be careful not to X" appears in a prompt | → **guard** (prompts are not enforcement) |
| a value you keep editing between runs | → **param** (or a **dim** if mode-shaped) |
| an agent step that always runs the same command | → **`run:` step** |
| a mid-run decision you make every time | → **gate** with a `default` |
| a setup paragraph in this README | → **`[tools]` check** or **`manual:` step** |
| a run that failed the same way twice | → validator first, then guard if it's dangerous |

Full guide: `docs/TOOLING-AND-GROWTH.md` in the cairn repo. The loop is always **author → plan (static
verify) → smoke-test a slice (`--from/--to`) → harden (promote checks to validators)**.

## Closing the loop — `cairn run self-improve`

The ladder is powered by noticing; `self-improve` makes the noticing systematic. It
aggregates every run's `learn` events (`cairn learnings`), has the curator agent judge them
against the doctrine — noise by default; the policy is yours in
`skills/self-improve-curator/SKILL.md` — and then STOPS at a human gate. Only an explicit
"yes" lets the final step apply the approved edits, on a new branch, as a PR via `gh`
(declared in `[tools.gh]`, so `cairn run` refuses up front when it's missing). Never a
direct commit: the loop writes suggestions, not truth. Headless runs (cron, `cairn batch`)
resolve the gate to its default "no", so an unattended run can never self-promote.
