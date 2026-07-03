# cairn — API Reference

Complete file-format and protocol reference. Everything a workspace author or executor implementer
needs; semantics live in `ARCHITECTURE.md`.

---

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
setup = "scripts/setup-grok-config.sh"   # doctor runs/points to this (BYOK effort aliases)
[executors.grok.tiers]
reasoning = { model = "grok-4.3-high" }  # alias bakes effort — Grok has no effort flag
balanced  = { model = "grok-build-med" }
cheap     = { model = "grok-build-low" }
```

Effort values are the shared enum `low | medium | high | xhigh`; a tier entry may fix effort (alias
executors like Grok) or accept the agent's `effort:` (flag executors like Claude/Codex).

## 2. Pipeline file — `pipelines/<name>.yaml`

### 2.1 Header

```yaml
pipeline: brease-rebuild
version: 1

params:                          # the CLI surface: --param k=v
  url:   { type: string, required: true }
  mode:  { type: enum, values: [rebuild, redesign, reimagine], default: rebuild }
  pages: { type: string, default: gate }          # <n> | all | gate
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
  when: params.pages == 'gate'
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
| Run dir name | the pipeline's `run_id:` template | collision ⇒ auto `-v2` suffix; `--idempotent` resumes instead |
| Artifact paths inside the run | each artifact's `path:` (template-capable) | the *only* author-controlled layout inside a run |
| The run skeleton (`run.json`, `trail.jsonl`, `gates/`, `logs/`) | **not configurable, on purpose** | the legibility invariant: every cairn run dir on earth reads the same (ARCHITECTURE §11) |

**Template mini-language** (one syntax, used by `run_id:`, `artifact.path:`, `args:` values, and
`run:`/`manual:` command strings):

```
VALUE placeholders     {params.<name>}  {dims.<key>}  {pipeline}  {date}=YYYYMMDD
                       {datetime}=YYYYMMDD-HHMM  {cycle} (loop bodies only)
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

**Strict vs lenient contexts.** `run_id`, `artifact.path:`, and `args:` values are rendered
**strictly**: any `{…}` that isn't a recognized cairn placeholder is a plan-time error, so a
typo can never slip through. But `run:`/`manual:` command strings embed foreign syntax — a `jq`
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
effort: medium                   # low | medium | high | xhigh (flag executors)
escalate:                        # optional conditional tier bump
  when: "dims.design != 'reproduce'"
  tier: reasoning
skills: [brease-capture-site, crawl4ai]
tools:
  allow: [read, write, edit, bash]
  bash: allowlist.yaml#capture   # command allowlist fragment (rendered per executor)
  network: true                  # default false — executor maps to its sandbox (SECURITY.md §3)
env: []                          # secrets passed to this agent's processes — deny by default;
                                 # names must be declared in [secrets] (SECURITY.md §1)
returns: schemas/step-return.json   # default; override for special agents
```

## 4. Validator & guard-check contract

Any executable. Validators: `argv = [run_dir, artifact_name, artifact_path]`; exit 0 pass; exit 1
fail with one reason per stdout line (fed to trail, halt message, and retry envelopes). `artifact_path`
is the artifact's rendered, run-dir-relative path (a glob artifact receives its pattern, e.g.
`blueprints/**`), so a generic validator can locate the file without knowing the logical name. Guard
checks:
`stdin = {"command": "...", "env": {...}, "run_dir": "..."}` JSON; exit 0 allow; exit 2 deny with
stderr reason. Both must be side-effect-free.

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
    blocking_hooks: bool | None     # None = unknown → doctor probes empirically
    output_schema: bool             # native typed-return support (used as bonus only)
    session_capture: str | None     # glob of session files to copy into logs/, if any

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

@dataclass
class Result:
    step: dict | None               # parsed STEP block (None if unparsable)
    exit_code: int
    duration_s: float

class Executor(Protocol):
    name: str
    capabilities: Capabilities
    def doctor(self) -> list[Finding]: ...                    # auth, version, hook probe
    def resolve_model(self, tier: str, effort: str) -> tuple[str, str | None]: ...
    def invoke(self, inv: Invocation) -> Result: ...          # ONE subprocess, blocking
    def install_guards(self, guards: list[Guard], ws: Workspace) -> None: ...
    def render_workspace(self, ws: Workspace) -> None: ...    # AGENTS.md etc., idempotent
```

Built-in `invoke` shapes (flags re-verified at doctor time; vendors drift):

```
claude:  claude -p "$(cat envelope)" --model {model} --effort {effort} --output-format json
codex:   codex exec -C {cwd} -m {model} -c model_reasoning_effort={effort}
             --sandbox workspace-write -a never --output-schema {return_schema} - < envelope
grok:    grok -p --cwd {cwd} -m {model} --output-format json --permission-mode dontAsk
             --no-alt-screen --no-auto-update < envelope
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

## 8. Run directory layout & schemas

```
runs/<run-id>/
├── run.json            # schemas/run.schema.json (below)
├── trail.jsonl         # append-only events (below)
├── gates/<name>.json
├── logs/<step>[.rN][.cK].{log,prompt.md}     # rN = retry, cK = loop cycle
└── <artifacts as declared>
```

### 8.1 `run.json` (pinned — resolves today's live drift)
```json
{ "run_id": "acme-redesign-20260702",
  "pipeline": "brease-rebuild", "pipeline_hash": "sha256:…", "cairn_version": "0.1.0",
  "params": { "url": "…", "mode": "redesign", "pages": "gate", "brease": "off" },
  "dims":   { "content": "keep", "design": "redesign", "brand": "keep", "routes": "keep" },
  "executors": { "default": "codex", "overrides": { "review": "claude" },
                 "versions": { "codex": "0.138.0" } },
  "models":  { "capture": "gpt-5.4/medium", "review": "opus/high" },
  "created_at": "…", "status": "running | done | halted",
  "nodes": { "<node-id>": { "status": "done|skipped|halted", "at": "…", "cycles": 2 } } }
```

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
           [--idempotent]                       # existing run_id ⇒ resume-or-no-op (SCHEDULING.md §3)
cairn resume <run-dir> [--force]        # accept pipeline-hash drift explicitly
cairn gate <run-dir> <name>=<choice>    # answer a pending gate externally (writes gates/<name>.json,
                                        # by:"external") — the operator-pattern hook for coding agents:
                                        # run exits 6 at an unanswered gate → operator asks the human in
                                        # its own UI → cairn gate … → cairn resume
cairn validate <run-dir> [artifact]
cairn test [validators|guards|pipelines|envelopes] [--pipeline P] [--update]
                                        # L1 offline suite: fixtures + stub runs + envelope snapshots
                                        # (full spec: TESTING.md)
cairn test record <run-dir> [--slim]    # harvest a real run into tests/stubs + tests/fixtures
cairn compose <pipeline> <step> [--param k=v]...   # render a step's envelope without executing
cairn trail <run-dir> [--watch] [--follow --json [--since SEQ]]
                                        # --follow --json = the canonical NDJSON stream for monitor
                                        # clients; --since resumes from a consumer offset
cairn ps [--workspace .] [--json]       # cross-run fleet view (running/gate-waiting/halted) —
                                        # derived from run.json + trail recency; no daemon, no registry
cairn doctor [--executor X] [--probe-hooks]
cairn batch <pipeline> --params-file sites.jsonl [-j 8] [--gate NAME=CHOICE]...
cairn learnings [--since DATE] [--tag TAG]      # aggregate learn events across all runs
                                                # (the learning loop: TOOLING-AND-GROWTH §7)
cairn gc [--keep-days N] [--keep-last M] [--artifacts-only]   # retention — never automatic
cairn schedule install|list|run <name>|uninstall [--backend cron|launchd|systemd]
                                                # sync schedules.yaml → host scheduler; the installed
                                                # entry always calls `cairn schedule run <name>` (SCHEDULING.md)
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
