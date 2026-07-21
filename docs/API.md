# cairn — API Reference

Complete file-format and protocol reference. Everything a workspace author or executor implementer
needs; semantics live in `ARCHITECTURE.md`.

---

## 0. Workspace layout — the orientation map

Every path in this reference is relative to the workspace root. This is exactly what `cairn new
workspace <name>` scaffolds — a workspace that plans *and* runs `hello` offline the moment it exists,
already carrying the `self-improve` learning-loop furniture:

```
<name>/
├── cairn.toml                     # §1 — the one config file; every relative path resolves here
├── README.md                      # the workspace's own docs (smoke target, tiers to wire, …)
├── allowlist.yaml                 # bash command allowlist fragments, referenced by agents (§3)
├── pipelines/                     # §2 — the trails
│   ├── hello.yaml                 #   day-0: plans + runs offline, zero auth (run: steps + one gate)
│   └── self-improve.yaml          #   the curate→promote learning loop (TOOLING-AND-GROWTH §7)
├── agents/                        # §3 — worker declarations (tier · effort · skills · tools)
│   ├── assistant.yaml
│   └── curator.yaml
├── skills/                        # markdown capability packs, inlined into envelopes (CONCEPTS §7)
│   ├── cairn-operator/SKILL.md
│   └── self-improve-curator/SKILL.md
├── schemas/                       # JSON Schemas for artifacts + the STEP return
│   ├── greeting.json
│   ├── step-return.json           # §7
│   └── self-improve-proposals.json
├── validators/                    # §4 — pure acceptance checks (argv: run_dir, name, path)
│   ├── nonempty.py
│   └── self-improve-proposals.py
├── prompts/
│   └── DOCTRINE.md                # workspace doctrine, inlined into every agent envelope
├── scripts/                       # helper scripts a run: step may shell out to
│   └── self-improve-open-pr.py
├── tests/                         # §TESTING — the offline L1 suite
│   ├── matrix.yaml                #   param sets the stub runs exercise
│   ├── fixtures/proposals/…       #   valid-*/invalid-* validator fixtures
│   └── stubs/self-improve/…       #   canned artifacts for stub runs
└── runs/                          # §8 — created on first run; every execution, self-describing (gitignored)
```

Two directories are conventional, added when a workspace needs them (they are *not* in the day-0
scaffold): **`guards/`** holds command-policy checks (§5), and **`tests/guards/`** their fixtures.
The run skeleton under `runs/<id>/` (§8) is fixed and not author-configurable — the legibility
invariant (ARCHITECTURE §11).

## 1. `cairn.toml` — workspace config

```toml
[workspace]
name        = "brease-factory"
doctrine    = "prompts/DOCTRINE.md"
runs_dir    = "runs"
default_executor = "claude"

[defaults]
step_timeout = "30m"
trail_context = { events = 12, learnings = 5 }   # envelope block 4 sizing

# ---- external tools: doctor-verified machine preflight (full doc: TOOLING-AND-GROWTH.md) ----
[tools.crawl4ai]
check   = "uv run python -c 'import crawl4ai'"   # exit 0 = present/authed
install = "uv sync && uv run playwright install chromium"
[tools.vercel]
check     = "vercel whoami"
install   = "pnpm add -g vercel && vercel login"
needed_by = ["deploy"]                           # doctor/plan scope failures to these steps

# ---- model tier resolution: the ONLY place vendor model names appear ----
[executors.claude]
enabled = true
[executors.claude.tiers]
reasoning = { model = "opus",   effort = "high" }
balanced  = { model = "sonnet", effort = "medium" }
cheap     = { model = "haiku",  effort = "low" }

[executors.codex]
enabled = true
pin_version = "0.138"                    # doctor warns off-version
[executors.codex.tiers]
reasoning = { model = "gpt-5.5",      effort = "high" }
balanced  = { model = "gpt-5.4",      effort = "medium" }
cheap     = { model = "gpt-5.4-mini", effort = "low" }
[executors.codex.flags]                  # extra argv the executor appends
sandbox = "workspace-write"
approvals = "never"

[executors.grok]
enabled = true
[executors.grok.tiers]                   # native --effort flag (grok 0.2.82) — no alias config
reasoning = { model = "grok-build",             effort = "high" }
balanced  = { model = "grok-build",             effort = "medium" }
cheap     = { model = "grok-composer-2.5-fast", effort = "low" }
```

Effort values are the shared enum `low | medium | high | xhigh | max`. Precedence is specific-over-general:
a fired `escalate.effort` (§3) wins, then an agent's own `effort:`, then a tier entry's `effort` as
the fallback used when the agent pins none; if none of them set it, effort is `None` and the executor
applies its own default. So a tier may supply a default effort for the agents that omit one, but it
never overrides an agent that pinned its own. All three vendor executors take effort as a flag
(Claude/Codex natively; Grok since 0.2.82's headless `--effort`, which takes exactly this enum).

## 2. Pipeline file — `pipelines/<name>.yaml`

### 2.1 Header

```yaml
pipeline: brease-rebuild
version: 1

params:                          # the CLI surface: --param k=v
  url:   { type: string, required: true }
  mode:  { type: enum, values: [rebuild, redesign, reimagine], default: rebuild }
  pages: { type: string, default: all }           # <n> | all — the scope gate always asks (headless → default)
  brease:{ type: enum, values: ["on", "off"], default: "off" }   # quote — see the note below

dims:                            # derived config: preset table over one param
  from: mode
  presets:
    rebuild:   { content: keep,    design: reproduce, brand: keep, routes: keep }
    redesign:  { content: keep,    design: redesign,  brand: keep, routes: keep }
    reimagine: { content: rewrite, design: redesign,  brand: new,  routes: restructure }

run_id: "{slug(params.url)}-{params.mode}-{date}"   # + auto -v2 suffix on collision
```

> **Quote YAML-boolean enum values.** Bare `on`/`off`/`yes`/`no`/`true`/`false` are
> parsed as *booleans* by YAML 1.1, but cairn resolves params to and compares them as
> **strings** — so an unquoted `default: off` becomes `False` and the pipeline's own
> `params.brease == 'off'` never matches (the branch silently mis-fires). Always quote
> such enum values, defaults, and gate-option keys: `values: ["on", "off"], default: "off"`.

### 2.2 Artifacts

```yaml
artifacts:
  discovery:      { path: captures/discovery.json,      schema: schemas/discovery.json }
  site-map:       { path: captures/site-map.json,       schema: schemas/site-map.json,
                    validator: validators/p0.py,
                    describe: "every page: url, type, sections[], images[]" }   # → envelope contract
  blueprints:     { path: "blueprints/**",              validator: validators/p2.py }
  art-review:     { path: "qa/art-review-r{cycle}.json", schema: schemas/art-review.json }
```

Keys: `path` (run-dir-relative; `**` globs allowed; `{cycle}`/`{param}` substitution), `schema`
(JSON Schema, optional), `validator` (executable, optional; at least one of schema/validator
required), `describe` (copied into agent envelopes).

### 2.3 Step node

```yaml
- id: capture                    # unique; trail + logs key
  agent: site-extractor          # XOR run: | manual:
  args: { step: capture }        # exposed to envelope + skill as {{args.*}}
  needs: [discovery, scope]      # artifact names (gate names are artifacts too)
  needs_optional: [strategy-brief]  # consumed if a prior conditional step produced it
  produces: [site-map, design-signals]
  when:  params.brease == 'on'   # optional; unless: also available
  timeout: 45m                   # overrides defaults.step_timeout
  retry: { attempts: 1, feedback: true }   # default {attempts: 0}
  executor: claude               # optional hard pin (else global/--step-executor)
  skippable: false               # true ⇒ STEP status:skipped satisfies the node
```

`run:` steps — deterministic, no agent, no envelope:
```yaml
- id: select-urls
  run: "uv run python skills/crawl4ai/scripts/discover_urls.py --select {gate:scope} --pages {params.pages} --out {artifact:selected-urls}"
  needs: [discovery, scope]
  produces: [selected-urls]
```
Command strings use the template mini-language (§2.8): `{artifact:X}`/`{gate:X}`/`{run_dir}`
resolve references, `{params.x}`/`{dims.x}` resolve values; command runs with `cwd = run dir`,
same timeout/log/trail treatment.

`manual:` steps:
```yaml
- id: brease-auth
  manual: "Run `brease login` and `brease use {param:brease_site}` in another terminal."
  produces: [brease-context]     # e.g. .brease/context.json existence+validator
```

### 2.4 Gate node

```yaml
- gate: scope
  reads: [discovery]             # summarized to the operator by the gate UI
  ask: "Which pages should we capture?"
  options:
    recommended: "nav-linked home/core/product pages"
    all:         "everything discovered"
    core:        "home + core only"
  default: all                   # headless resolution; also `cairn run --gate scope=all`
```
Decision lands at `gates/scope.json` `{ "choice": "...", "by": "tty|default|flag", "at": ... }` and
is referenced in `needs:` by gate name.

A gate *may* carry a `when:`, but then it is inactive whenever that condition is false — so every
consumer of `{gate:X}`/`gates.X` must be guarded by the **same** condition, or it breaks at runtime.
The planner enforces this: an unguarded consumer of a conditional gate is a plan error, and a
consumer whose guard can't be shown to include the gate's condition is a plan warning. Prefer an
**unconditional** gate with a `default:` (as above) — it always resolves (headless → the default),
so consumers need no guard at all.

### 2.5 Parallel node

```yaml
- parallel: blueprint
  on_fail: wait_all              # | fast
  steps: [ {id: architect, ...}, {id: design-author, ...} ]   # disjoint produces (plan-checked)
```

### 2.6 Loop node

```yaml
- loop: art-review
  when: dims.design != 'reproduce'
  min: 1
  max: { interactive: 3, headless: 2 }
  until: artifacts.art-review.verdict == 'approve'   # evaluated after each cycle ≥ min
  on_cap: continue               # | halt
  body:
    - { id: review, agent: design-director, args: {job: review}, needs: [frontend, design-md], produces: [art-review] }
    - { id: revise, agent: frontend-builder, args: {revision: on}, needs: [art-review], produces: [frontend],
        unless: artifacts.art-review.verdict == 'approve' }
```
`{cycle}` is bound in body artifact paths, args, and expressions. Loop bodies may re-`produce` an
artifact earlier steps produced (the only sanctioned re-production).

### 2.7 Guards block

```yaml
guards:
  - name: no-screenshot-media
    match: { tool: bash, command: "brease* createMedia*" }
    check: guards/f18.py
    enforce: [hook, shim, post]
    on_error: allow              # fail-open | deny (fail-closed)
  - name: wrong-cms-target
    match: { tool: bash, command: "brease*" }
    check: guards/cms-target.py  # compares active context to run.json.brease_target
    enforce: [hook, shim]
    when: params.brease == 'on'
```

### 2.8 Naming, locations & the template mini-language

**What is configurable, where:**

| Output | Configured by | Notes |
|---|---|---|
| Where run dirs live | `cairn.toml → [workspace] runs_dir` (absolute or workspace-relative) | one setting per workspace; batch children land under it too |
| One run's exact dir | `cairn run --run-dir PATH` | per-invocation escape hatch (CI temp dirs, tests) |
| Run dir name | the pipeline's `run_id:` template | collision ⇒ auto `-v2` suffix; `--idempotent` matches an equivalent run by a `(pipeline, params, {date})` content key (via `schedkit.find_idempotent_run`) and resumes/no-ops it instead of minting a variant |
| Artifact paths inside the run | each artifact's `path:` (template-capable) | the *only* author-controlled layout inside a run |
| The run skeleton (`run.json`, `trail.jsonl`, `gates/`, `logs/`) | **not configurable, on purpose** | the legibility invariant: every cairn run dir on earth reads the same (ARCHITECTURE §11) |

**Template mini-language** (one syntax, used by `run_id:`, `artifact.path:`, `args:` values, and
`run:`/`manual:` command strings):

```
VALUE placeholders     {params.<name>}  {dims.<key>}  {pipeline}  {date}=YYYYMMDD (UTC)
                       {datetime}=YYYYMMDD-HHMM (UTC)  {cycle} (loop bodies only)
REFERENCE placeholders {artifact:<name>} → the artifact's ABSOLUTE path
                       {gate:<name>}     → the recorded choice value
                       {run_dir}         → the run dir's absolute path
Helpers                {slug(params.url)}   hostname → kebab, www/TLD stripped
                       {dash(params.variant)}  "-<v>" if non-empty, else ""
                       {short(<value>, n)}  first n chars
```

Resolution rules: a missing value is a **plan-time error**, never a silent empty (same rule as
expressions — misspellings must not quietly produce `acme--20260703`); helpers are the fixed set
above (no user-defined functions — that's what `run:` steps are for); `{artifact:…}` in
`artifact.path:` is illegal (paths can't depend on other paths).

**Strict vs lenient contexts.** `run_id` and `artifact.path:` are rendered **strictly**: any
`{…}` that isn't a recognized cairn placeholder is a plan-time error, so a typo can never slip
through. But `run:`/`manual:` command strings and `args:` values embed foreign syntax — a `jq`
filter, an `awk` program, a Python dict literal — that legitimately contains braces, so they are
rendered **leniently**: only the recognized cairn placeholders
(`{params.x}`/`{dims.x}`/`{artifact:x}`/`{gate:x}`/`{run_dir}`/`{cycle}`/`{pipeline}`/helpers) are
substituted and everything else passes through verbatim. Typos *inside* a recognized placeholder
(`{artifact:typ0}`, `{params.mdoe}`) are still caught in both modes — leniency only spares braces
that were never cairn's to begin with.

**Outputs *outside* the run dir are deliberately not config.** The isolation doctrine (a run
writes only inside its run dir) is load-bearing for resume, audit, and concurrency — so exporting
a deliverable (e.g. copy the built frontend to a shared demo folder) is an explicit final `run:`
step (`rsync {artifact:frontend}/ /srv/demos/{params.mode}/…`), visible in the plan, gated by
validators like everything else — never a config knob that quietly writes elsewhere.

## 3. Agent file — `agents/<name>.yaml`

```yaml
description: "P0 worker: crawls the source site into structured captures/"
tier: balanced                   # reasoning | balanced | cheap
effort: medium                   # low | medium | high | xhigh | max — wins over the tier's effort (§1)
escalate:                        # optional conditional tier bump
  when: "dims.design != 'reproduce'"
  tier: reasoning
  effort: xhigh                  # optional; when the escalation fires this beats the agent's
                                 # effort (and the tier's). Omitted → the agent's effort stands.
skills: [brease-capture-site, crawl4ai]
tools:
  allow: [read, write, edit, bash]
  bash: allowlist.yaml#capture   # command allowlist fragment (rendered per executor)
  network: true                  # default false — executor maps to its sandbox (SECURITY.md §3)
env: []                          # secrets passed to this agent's processes — deny by default;
                                 # names must be declared in [secrets] (SECURITY.md §1)
returns: schemas/step-return.json   # default; override for special agents
```

The keys above (plus `description`) are the whole surface cairn reads — an agent file is config,
not a prompt. Any other key (e.g. `prompt:`, `mission:`) is **ignored with a plan warning** naming
the key and file: behavior belongs in a skill (loaded into the envelope), never in agent config, so
a stray behavior key is silent data loss unless surfaced.

## 4. Validator & guard-check contract

Any executable. Validators: `argv = [run_dir, artifact_name, artifact_path]`; exit 0 pass; exit 1
fail with one reason per stdout line (fed to trail, halt message, and retry envelopes). `artifact_path`
is the artifact's rendered, run-dir-relative path (a glob artifact receives its pattern, e.g.
`blueprints/**`), so a generic validator can locate the file without knowing the logical name. Guard
checks:
`stdin = {"command": "...", "env": {...}, "run_dir": "..."}` JSON; exit 0 allow; exit 2 deny with
stderr reason. Both must be side-effect-free. `env` is the **`CAIRN_*`-only safe subset** of the
step's environment (`guards._safe_env`) — secrets a step holds never cross to a check process.

## 5. Expression grammar (the whole thing)

Deliberately tiny; parsed with a ~100-line recursive-descent parser, **never** `eval`:

```
expr    := or ; or := and ('||' and)* ; and := cmp ('&&' cmp)*
cmp     := value (('=='|'!='|'in') value)?
value   := literal | path | '!' value | '(' expr ')'
path    := (params|dims|artifacts|gates|run|cycle) ('.' ident)*
literal := 'string' | number | true | false
```
`artifacts.X.Y.Z` lazily loads artifact X's JSON and walks it. Missing path = error (not falsy) —
misspellings must not silently disable steps.

## 6. Executor protocol (Python)

```python
@dataclass
class Capabilities:
    blocking_hooks: bool | None     # CLI-capability/probe question: None = unknown → doctor probes
    output_schema: bool             # native typed-return support (used as bonus only)
    session_capture: str | None     # glob of session files to copy into logs/, if any
    installs_hooks: bool            # IMPLEMENTATION fact: does cairn's install_guards for this
                                     # executor actually wire a pre-execution blocking hook (True
                                     # only for claude today; W3b, ARCHITECTURE §4) — distinct from
                                     # blocking_hooks (whether the vendor CLI *can*, at all)

@dataclass
class Invocation:
    prompt_file: Path               # the rendered envelope
    model: str                      # already tier-resolved
    effort: str | None              # None when baked into model alias
    cwd: Path                       # the run dir
    env: dict[str, str]             # CAIRN_RUN_DIR etc. + guard shims on PATH
    timeout_s: int
    log_path: Path
    return_schema: Path
    network: bool = False           # the step's resolved network policy (StepNode.network,
                                    # plan.py) — default false. codex consumes it today (`-c
                                    # sandbox_workspace_write.network_access=...`, W5b); grok's
                                    # sandbox profile has no separate network toggle to verify
                                    # against, claude's CLI has none at all — both leave it
                                    # unconsumed on purpose, not a silent drop (ARCHITECTURE §5)

@dataclass
class Result:
    step: dict | None               # parsed STEP block (None if unparsable)
    exit_code: int
    duration_s: float
    usage: dict | None = None       # executor-reported tokens/cost; outranks the model's
                                    # STEP-block self-report. All CLI executors run plain-text
                                    # output today and pass None — a json output-format is the
                                    # future source; the stable schema is the value now

class Executor(Protocol):
    name: str
    capabilities: Capabilities
    def doctor(self) -> list[Finding]: ...                    # auth, version, hook probe
    def resolve_model(self, tier: str, effort: str) -> tuple[str, str | None]: ...
    def invoke(self, inv: Invocation) -> Result: ...          # ONE subprocess, blocking
    def install_guards(self, guards: list[Guard], ws: Workspace) -> None: ...
    def render_workspace(self, ws: Workspace) -> None: ...    # AGENTS.md etc., idempotent
```

Built-in `invoke` shapes (as actually built; flags re-verified at doctor time — vendors drift).
`[--effort …]`/`[-c …]` appear only when the step resolves an effort:

```
claude:  claude -p --model {model} [--effort {effort}] --output-format text
             --permission-mode bypassPermissions
             --setting-sources project --strict-mcp-config --no-session-persistence
             < envelope
             (prompt on stdin — the positional `prompt` arg is omitted, so `-p`/`--print` reads
              stdin instead; keeps the envelope off `ps`/`/proc/*/cmdline` and off the argv
              `MAX_ARG_STRLEN` (128 KiB) ceiling a skill-heavy envelope could trip. bypassPermissions
              is required: headless `claude -p` under the default mode refuses every tool use and
              exits 0 without producing the artifact — cairn's blocking PreToolUse guards are the
              enforcement layer instead of an interactive prompt. `--setting-sources project` seals
              the process from the user's ambient `~/.claude/settings.json` while keeping the
              run-dir `.claude/settings.json` install_guards writes (that IS the "project" source);
              `--strict-mcp-config` drops any ambient MCP servers; `--no-session-persistence`
              disables the on-disk session transcript entirely — session_capture is None, there is
              nothing to capture)
codex:   codex exec -C {cwd} -m {model} --sandbox workspace-write --skip-git-repo-check
             --ephemeral --ignore-user-config --ignore-rules
             -c sandbox_workspace_write.network_access={true|false}
             [-c model_reasoning_effort={effort}]  < envelope
             (prompt on stdin; `-a/--ask-for-approval` is gone from `codex exec` as of codex-cli
              0.142.5 — exec hardwires approval-never; `--skip-git-repo-check` because codex refuses a
              non-git/untrusted cwd and cairn's `--sandbox` flag + guards are the enforcement layer;
              `--ephemeral` (W5b) runs without persisting session files, so session_capture is None
              — nothing under ~/.codex/sessions/** to capture; `--ignore-user-config` skips
              `$CODEX_HOME/config.toml` (auth still uses CODEX_HOME), `--ignore-rules` skips
              user/project execpolicy `.rules` files — both seal the process from ambient config;
              `-c sandbox_workspace_write.network_access=...` (W5b, codex-F5) threads
              Invocation.network through, emitted unconditionally so `false` is stated
              explicitly, not left to the sandbox's undeclared default; --output-schema is NOT
              wired yet — the STEP sentinel is the contract)
grok:    grok --prompt-file {envelope} --cwd {cwd} -m {model}
             --output-format plain --permission-mode bypassPermissions
             --no-alt-screen --no-auto-update --no-memory --sandbox workspace
             [--effort {effort}]
             (grok 0.2.101: headless mode does NOT read stdin — the envelope is delivered via
              --prompt-file; `--output-format text` is gone (valid: plain|json|streaming-json);
              bypassPermissions is required — `dontAsk` silently denies file writes (exit 0,
              empty output, no artifact) and PreToolUse hooks still apply under bypass;
              `--effort low|medium|high|xhigh|max` is grok's native headless effort flag — NOT
              `--reasoning-effort`, a separate per-model knob; native --json-schema exists but
              is NOT wired — the STEP sentinel is the contract; `--no-memory` disables
              cross-session memory and `--sandbox workspace` applies the built-in workspace-write
              equivalent sandbox profile — both seal the process from ambient config/access)
shell:   the run: command verbatim (this executor is how deterministic steps execute)
stub:    copies tests/stubs/<pipeline>/<step>[.c<cycle>]/ into the run dir + returns a canned
         STEP — the L1 test executor (TESTING.md §5); selectable like any other, so a full
         pipeline can be "run" offline

```

Registration: `[project.entry-points."cairn.executors"] myexec = "pkg.mod:MyExecutor"` — same
mechanism for gate UIs (`cairn.gates`) and trail sinks (`cairn.sinks`).

## 7. STEP return schema — `schemas/step-return.json`

```json
{ "type": "object",
  "required": ["status", "summary", "artifacts"],
  "properties": {
    "status":   { "enum": ["done", "skipped", "blocked"] },
    "summary":  { "type": "string", "maxLength": 2000 },
    "artifacts":{ "type": "array", "items": { "type": "string" } },
    "metrics":  { "type": "object" },
    "learnings":{ "type": "array", "items": { "type": "object",
                   "required": ["note"], "properties": {
                     "note": {"type": "string"}, "tag": {"type": "string"} } } },
    "blockers": { "type": "array", "items": { "type": "string" } } } }
```
Emitted between `<<<STEP` / `STEP>>>` sentinels as the final message. Authority rule: artifact
validation outranks this block in both directions (ARCHITECTURE §7).

`parse_step_sentinel` (`executors/base.py`) extracts this block defensively: it locates each
`<<<STEP` marker and reads the first complete JSON value after it with `json.JSONDecoder.raw_decode`
(real JSON parsing, not a regex to the closing marker), so a `STEP>>>` substring quoted inside a
`summary`/`note` string does not truncate the block. The parsed object is then validated against
this schema with `required` dropped — so a status-only block (e.g. `{"status": "blocked",
"summary": "…"}`, no `artifacts`) still parses, but a wrong-shaped field (e.g. `"learnings":
["x"]`, a bare string instead of an object with `note`) does not. The last marker that yields a
schema-valid object wins; anything that fails to parse or validate is skipped in favour of an
earlier well-formed block, and if none validates the result is `None` — a soft signal, never a
hard failure.

## 8. Run directory layout & schemas

The skeleton (`run.json`, `trail.jsonl`, `gates/`, `logs/`, and the internal `.cairn.lock` / `.cairn/`)
is identical in every cairn run on earth; only the artifact paths under it are author-controlled (each
artifact's `path:`). A populated run:

```
runs/acme-redesign-20260703/
├── run.json                       # the pinned manifest — schemas/run.schema.json (§8.1)
├── trail.jsonl                    # append-only event log — the authority (§8.2)
├── .cairn.lock                    # advisory flock — serializes resume/parallel writers (SECURITY §5)
├── .cairn/                        # kernel bookkeeping, not author-facing
│   └── step-return.json           #   the STEP return schema, materialized for the executor to read
├── gates/
│   └── scope.json                 # one decision file per answered gate ({choice, by, at})
├── logs/                          # one pair per step attempt — the exact record of what ran
│   ├── capture.prompt.md          #   the rendered envelope (the prompt IS an artifact)
│   ├── capture.log                #   the executor's stdout/stderr
│   ├── build.r2.prompt.md         #   .rN = retry attempt N (r2 = second attempt)
│   ├── review.c1.log              #   .cK = loop cycle K (art-review cycle 1)
│   └── review.c1.prompt.md
├── superseded/                    # withdrawn proof from `resume --from NODE` (§9): one
│   └── 20260705T101502Z/…         #   stamped snapshot per invalidation, relative paths preserved
└── captures/  blueprints/  qa/ …  # the artifacts themselves, at each artifact's declared path:
```

Log stems are `<step>[.rN][.cK].{log,prompt.md}`: the retry suffix `.rN` appears only from the second
attempt on (attempt 1 carries none), and the loop-cycle suffix `.cK` is present for **every** cycle of a
loop body — cycle 1 is `.c1` — and absent only for steps outside a loop.

### 8.1 `run.json` (pinned — resolves today's live drift)
```json
{ "run_id": "acme-redesign-20260702",
  "pipeline": "brease-rebuild", "pipeline_hash": "sha256:…", "cairn_version": "0.1.0",
  "params": { "url": "…", "mode": "redesign", "pages": "gate", "brease": "off" },
  "dims":   { "content": "keep", "design": "redesign", "brand": "keep", "routes": "keep" },
  "executors": { "default": "codex", "overrides": { "review": "claude" },
                 "versions": { "codex": "0.138.0" } },  // probed at mint; null on a failed probe
  "models":  { "capture": "gpt-5.4/medium", "review": "opus/high" },
  "git_rev": "…", "git_dirty": false,   // workspace HEAD + dirty flag at mint; both null outside git
  "created_at": "…", "status": "running | done | halted",
  "nodes": { "<node-id>": { "status": "running|done|skipped|halted", "at": "…", "cycles": 2 } } }
```
`at` is the node's real transition time (not the run's `created_at` clock) — `cairn ps` and a
post-mortem trail reflect when each node actually finished (ARCHITECTURE §10).

### 8.2 Trail events (one JSON per line — the Trail Protocol, full spec: OBSERVABILITY.md)

Versioned envelope: `{v, seq, at, run_id, event, node?, attempt?, cycle?, data{…}}` — single
writer, atomic flushed appends, `seq` strictly monotonic (the consumer offset).

```json
{"v":1,"seq":7,"at":"…","run_id":"acme-redesign-20260703","event":"step-start","node":"capture","attempt":1,"data":{"model":"gpt-5.4/medium","log_path":"logs/capture.log"}}
{"v":1,"seq":8,"at":"…","run_id":"…","event":"heartbeat","node":"capture","data":{"log_bytes":81234,"last_line":"crawled 12/19"}}
{"v":1,"seq":9,"at":"…","run_id":"…","event":"step-done","node":"capture","data":{"artifacts":["captures/site-map.json"],"metrics":{"pages":19},"duration_s":412,"usage":{"in_tokens":48211,"out_tokens":9107}}}
{"v":1,"seq":10,"at":"…","run_id":"…","event":"gate-pending","node":"scope","data":{"question":"Which pages should we capture?","options":["recommended","all","core"]}}
{"v":1,"seq":11,"at":"…","run_id":"…","event":"gate-answered","node":"scope","data":{"choice":"recommended","by":"tty"}}
{"v":1,"seq":12,"at":"…","run_id":"…","event":"run-halt","node":"build","data":{"reason":"artifact gate failed","validator_reasons":["section key 'heroX' not in catalog"],"exit_code":3}}
```

Full taxonomy (run/plan/step/gate/loop/guard/heartbeat/learn) in OBSERVABILITY.md §1.

## 9. CLI reference

```
cairn plan <pipeline> [--param k=v]... [--executor X] [--json]
cairn run  <pipeline> [--param k=v]... [--executor X] [--step-executor STEP=X]...
           [--gate NAME=CHOICE]... [--headless] [--to NODE] [--from NODE] [--run-dir PATH]
           [--idempotent]                       # match an equivalent run by the (pipeline, params,
                                                # {date}) content key: complete → no-op (exit 0);
                                                # incomplete → resume it (same drift guard as resume);
                                                # none → fresh run (SCHEDULING.md §3)
cairn resume <run-dir> [--force] [--from NODE]
                                        # --force accepts pipeline-hash/cairn-version drift AND re-pins
                                        # run.json to the present, so the consent holds across later resumes;
                                        # --from re-executes from NODE: its and every later node's records
                                        # are cleared and their still-valid artifacts (all loop cycles; gate
                                        # decisions too) move to superseded/<stamp>/ before the walk — the
                                        # escape hatch when a step's code was fixed after the step ran and
                                        # skip-if-done would keep serving the stale artifact
cairn gate <run-dir> <name>=<choice>    # answer a pending gate externally (writes gates/<name>.json,
                                        # by:"external") — the operator-pattern hook for coding agents:
                                        # run exits 6 at an unanswered gate → operator asks the human in
                                        # its own UI → cairn gate … → cairn resume
cairn validate <run-dir> [artifact]
cairn test [validators|guards|pipelines|envelopes] [--update]
                                        # L1 offline suite: fixtures + stub runs + envelope snapshots
                                        # (full spec: TESTING.md)
cairn test record <run-dir> [--slim]    # harvest a real run into tests/stubs + tests/fixtures
cairn compose <pipeline> <step> [--param k=v]... [--run-dir PATH]   # render a step's envelope without executing
cairn trail <run-dir> [--watch] [--follow --json [--since SEQ]]
                                        # --follow --json = the canonical NDJSON stream for monitor
                                        # clients; --since resumes from a consumer offset
cairn ps [--workspace .] [--json]       # cross-run fleet view (running/gate-waiting/halted) —
                                        # derived from run.json + trail recency; no daemon, no registry
cairn doctor [--executor X] [--probe-hooks]
                                        # machine preflight; --probe-hooks additionally spawns a throwaway
                                        # canary project per executor (native deny-hook + guarded side-effect)
                                        # and classifies whether its PreToolUse hook fires+blocks headless —
                                        # fires+blocks / fires-not-blocks / no-fire / inconclusive. Opt-in,
                                        # never cached (a fresh per-machine fact). Exit policy: a declared
                                        # blocking_hooks=True that the probe falsifies → error; None + a
                                        # concrete outcome → informational; inconclusive → warning, never error
cairn batch <pipeline> --params-file sites.jsonl [-j 8] [--gate NAME=CHOICE]... [--to P] [--from P]
                                        # process pool of `cairn run --headless` children, one per JSONL line;
                                        # --to/--from pass through to every child verbatim (same node-range
                                        # semantics + validation as `cairn run`), so a batch can farm just a
                                        # sub-range (e.g. the credential-free `--to P2`); a failed child's
                                        # summary names its reason — a bounded stderr tail + a run-dir pointer
cairn learnings [--since DATE] [--tag TAG]      # aggregate learn events across all runs, ranked
                                                # (the learning loop: TOOLING-AND-GROWTH §7)
cairn gc [--keep-days N] [--keep-last M] [--artifacts-only] [--include-needs-human] [--apply]
                                        # retention — never automatic; dry-run unless --apply
cairn schedule install|list|run <name>|uninstall [--backend cron|launchd|systemd]
           [--launchd-dir P] [--systemd-dir P]  # sync schedules.yaml → host scheduler; the installed
                                                # entry always calls `cairn schedule run <name>` (SCHEDULING.md)
cairn trigger sync|list|remove <name>|run <name> [--backend cron|launchd|systemd]
           [--launchd-dir P] [--systemd-dir P] [--workspace .] [--json]
                                                # sync triggers.yaml → host watcher (launchd WatchPaths /
                                                # systemd .path units); cron always refuses — no file-watch
                                                # facility (documented schedules.yaml poll fallback,
                                                # TRIGGERS.md §3). `run <name>` drains one trigger's inbox
                                                # now — also what the installed host unit calls when it
                                                # fires; its exit code covers claim/spawn hazards, not just
                                                # a failed child (TRIGGERS.md §2)
cairn new workspace|pipeline|agent|skill|validator <name>
```

`--to P2` / `--from P1` replace today's `run=..P2` range knob (node ids, inclusive). `--headless`
forces gate defaults + `max.headless` loop caps (batch implies it).

`cairn trail --watch` output sketch:
```
brease-rebuild · acme-redesign-20260702 · codex (review→claude)
  ✔ discover      2m01s   19 pages
  ✔ scope         gate    recommended (tty)
  ✔ capture       6m52s   19 pages · 214 images
  ✔ audit         3m10s   mode-plan: 4 keep · 2 merge · 1 drop
  ─ strategy      skipped (dims.content == keep)
  ▶ blueprint     running   architect 4m· ─ design-author 4m·
  ○ build · art-review ×≤3 · qa · deploy
```
