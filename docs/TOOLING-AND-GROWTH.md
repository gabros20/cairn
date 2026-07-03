# cairn — Tools & how a workspace grows

How external tools (crawl4ai, vercel, gh, brease, ffmpeg, …) enter a pipeline, and how a workspace
is built out incrementally — the lifecycle brease-factory actually lived, with names.

---

## 1. There is no "tool" object — on purpose

The kernel has five node kinds and zero tool abstractions. A tool is **four small declarations in
places that already exist**, each with exactly one job:

| Declaration | Job | Lives in |
|---|---|---|
| `[tools.X]` entry | *verify* — present, right version, authed | `cairn.toml` (§2) |
| a skill | *teach* — how agents drive it well | `skills/X/SKILL.md` (+ scripts/) |
| an allowlist fragment | *permit* — which invocations are allowed | `allowlist.yaml#fragment` |
| a step | *use* — agentic (`agent:` + skill) or deterministic (`run:`) | the pipeline |

Wrapping tools in framework objects (plugins, adapters, tool registries) is the road every agent
framework walks to bloat. A CLI on PATH plus knowledge, permission, and verification is the whole
requirement.

## 2. `[tools]` — machine preflight

```toml
# cairn.toml
[tools.crawl4ai]
check   = "uv run python -c 'import crawl4ai'"
install = "uv sync && uv run playwright install chromium"

[tools.vercel]
check     = "vercel whoami"            # presence AND auth in one probe
install   = "pnpm add -g vercel && vercel login"
needed_by = ["deploy"]                 # scopes doctor failures to the steps that care

[tools.gh]
check     = "gh auth status"
install   = "brew install gh && gh auth login"
needed_by = ["deploy"]

[tools.brease]
check     = "brease --version"
install   = "clone designatives/brease-cli && pnpm install && npm link"
needed_by = ["model-cms", "populate"]
```

Semantics:
- `cairn doctor` runs every `check`; a failure prints the `install` hint and its `needed_by`
  scope. Tool checks are **advisory** — a failing tool is a warning, never a hard exit on its own
  (doctor's exit is driven only by a workspace-lint error or a broken in-scope executor).
- *Built:* range-scoped tool enforcement — `cairn plan` warns when an in-range step's `needed_by`
  tools are unverified (a lazy workspace scan; zero subprocesses at plan time), and `cairn run`/
  `resume` hard-stop **before minting the run** when a scoped tool's check fails: one aggregated
  refusal naming every failing tool, exit `CONFIG`, nothing on disk (fail-fast beats a P6 crash
  after a 90-minute build). Unscoped tools stay doctor's advisory job; there is deliberately no
  skip flag. One deliberate ruling: resume re-checks tools even for status-done nodes, because the
  walk's artifact self-heal can re-execute them.
- **Machine setup vs run setup:** `[tools]` is per-machine (doctor's job). Per-run auth — like
  `brease login` scoped into a run dir — stays a `manual:` step in the pipeline, where it is
  checkable and resumable (see `brease-auth` in the example pipeline).

## 3. Permit: allowlist fragments + guards

```yaml
# allowlist.yaml — named fragments, referenced by agents as bash: allowlist.yaml#name
capture:
  - "uv run python skills/crawl4ai/scripts/*"
node-build:
  - "pnpm install*" 
  - "pnpm build*"
  - "pnpm lint*"
  - "npx tsc*"
deploy:
  - "vercel *"
  - "gh repo create designatives/*"
  - "gh api *"
readonly-plus-screenshot:
  - "npx agent-browser *"
```

The design: executors render fragments to their native permission surface (Claude
`permissions.allow`, Codex Rules, Grok `[permission]`) — authored once. Destructive verbs of an
allowed tool get a `guards:` entry on top (the F18 pattern): allowlist says *may run*, guard says
*checked before running*. *Status: today the planner parses the fragments (agents reference them
via `tools.bash`) but `render_workspace` emits only `CLAUDE.md`/`AGENTS.md`; rendering fragments to
executor-native permission formats lands with the per-executor milestones — see IMPLEMENTATION-PLAN.*

## 4. Use: agentic vs deterministic

**The decision rule: if you can write the exact command, it's a `run:` step. If driving the tool
requires judgment, it's an `agent:` step with the skill.**

```yaml
# deterministic — no model, same contracts
- id: deploy-vercel
  run: "vercel deploy --prod --yes --cwd {artifact:frontend}"
  needs: [frontend, qa-report]
  produces: [deploy-report]

# agentic — the model decides how to drive it
- id: capture
  agent: site-extractor           # agent has skills:[crawl4ai], bash: allowlist.yaml#capture
  needs: [discovery, selected-urls]
  produces: [site-map, design-signals]
```

Tools tend to **graduate downward**: they enter agentic (you don't yet know the right invocation),
and once the trail shows the agent running essentially the same command every run, you demote the
step to `run:` — cheaper, faster, deterministic. The trail is what makes the graduation visible.

## 5. The maturation ladder — how a workspace grows

brease-factory's actual history ("crawl4ai capture starter" → six-phase pipeline system), with the
promotions named. **Start sparse; promote when a run teaches you.**

| Signal from real runs | Promotion |
|---|---|
| the same prompt fragment recurring across steps | → **skill** |
| you check an output by eye every run | → **validator** (encode the check you were doing) |
| "be careful not to X" appears in a prompt | → **guard** (prompts are not enforcement) |
| a value you keep editing between runs | → **param** (or a **dim** if mode-shaped) |
| an agent step that always runs the same command | → **`run:` step** |
| a mid-run decision you make every time | → **gate** with a `default` |
| a setup paragraph in the README | → **`[tools]` check** or **`manual:` step** |
| a run that failed the same way twice | → validator first, then guard if it's dangerous |

Day-0 workspace is deliberately tiny: `cairn new workspace`, one `[tools]` entry, a two-step
pipeline, `nonempty.py` as the only validator. Ship the walking skeleton; let the trail drive the
rest. The framework never punishes sparseness — validators, guards, gates, and skills are all
*additive* declarations.

## 6. Building out with a coding agent — authoring, not just operating

A workspace is entirely files with schemas, which makes a coding agent a first-class **workspace
author** — and **`cairn plan` is its typecheck**:

```
you: "add a lighthouse audit step after build"
agent: edits pipelines/…yaml, adds agents/lighthouse-auditor.yaml + allowlist fragment
agent: cairn plan brease-rebuild --param mode=redesign
plan:  ✗ step 'lighthouse' needs artifact 'lh-report' declared but no schema/validator exists
agent: writes schemas/lh-report.json, re-plans → green
agent: cairn run … --from build --to lighthouse        # smoke-test just the new slice
```

The loop is: **author → plan (static verify, zero tokens) → smoke-test a slice (`--from/--to`) →
harden (promote checks to validators)**. This is exactly how this repo was built with Claude Code —
minus the part where a config mistake cost a full pipeline run to discover. `plan`'s file+line
errors and the trail's per-step evidence are designed to be *agent-legible*: the authoring agent
diagnoses from the same artifacts the operating agent and the human do.

Recommended workspace furniture: a short `skills/workspace-authoring/SKILL.md` teaching *your*
coding agent the house style (where fragments live, validator conventions, when to promote) — the
CLAUDE.md of pipeline authorship. cairn doesn't need it; your agents profit from it.

## 7. The learning loop — closing `learn` back into the workspace

The maturation ladder (§5) is powered by *noticing*. The learning loop is noticing made
systematic — the cairn re-host of the current repo's `learnings.jsonl` + self-improver mechanism:

```
runtime      STEP.learnings[] → trail `learn` events            (agents record as they work)
recall       envelope block 4 injects top-K learnings           (the next run already knows)
aggregate    `cairn learnings [--since] [--tag]`                (scan all trails, dedupe, rank)
curate       a human or agent reviews: which learnings are      (judgment — never automatic)
             ladder promotions (§5) vs noise?
promote      edits to skills/validators/guards/params           (the ladder, executed)
             — on a branch, as a PR — never committed directly
```

*Status: all five rows are built. `runtime`/`recall`/`aggregate` — agents emit `learn` events,
envelope block 4 injects prior learnings, and the `aggregate` verb (`cairn learnings [--since]
[--tag]`) scans every run's trail under the runs root and renders the ranked, deduped view (LIVE).
The `curate`/`promote` rows shipped as **scaffolded workspace furniture** — the framework ships
the mechanism, the workspace owns the policy; deliberately NOT a kernel verb and NOT a vendor
skill (see IMPLEMENTATION-PLAN).*

Two closure speeds, both now built: **runtime closure** (block 4 — a learning recorded in
run N is in run N+1's envelope with zero human action) and **design-time closure** (curation →
ladder promotion → the learning becomes *structure* and its note can be retired).

The curate→promote stage *is* a cairn pipeline, and it ships in the workspace scaffold
(DISTRIBUTION §4; retrofit into an existing workspace with `cairn new pipeline self-improve`).
The shipped shape of `pipelines/self-improve.yaml`: an `aggregate` `run:` step (`cairn learnings
--since/--tag` → a typed snapshot); a `curate` agent step judging against the doctrine skill
(`skills/self-improve-curator/SKILL.md` — vendor-free and decidable: noise by default, only
recurring (≥2) or high-value learnings promote, and the ladder table carries the exact promotion
enum tokens) into schema+validator-checked `proposals.json`; an `approve` human gate whose
headless default is **no** — a cron-fired run can never self-promote, and the scaffold's own
recorded test proves it; and an `open-pr` `run:` script that applies only approved, re-validated,
workspace-relative targets (no `../`, no absolute paths, no `runs/`/`.git`/`.env`, with a
resolved-path backstop) inside a **temporary git worktree** — the working branch is structurally
untouchable — on branch `self-improve/<run-id>`, opening the PR via `gh`; no approved edits means
exit 1, not an empty PR, and a failed push keeps the branch for retry (a documented asymmetry).
The scaffold's `[tools.gh] needed_by=["open-pr"]` dogfoods §2's tool enforcement. The framework
improving its own workspace with the same contracts, gates, and audit trail as any other work —
and the hard rules carried over from the current system's self-improver are unchanged:
**proposals arrive as branches/PRs, never as direct commits**; **curation is never automatic**;
**the human gate is mandatory** — the learning loop has write access to suggestions, not to truth.
