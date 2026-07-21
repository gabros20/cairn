# cairn — brease-rebuild, expressed completely

> **What this is.** A worked example, not a shipped cairn workspace. *brease-rebuild* is a real,
> six-phase website-rebuild pipeline that runs today as an **internal Claude-Code system**
> (brease-factory) — the origin pipeline cairn was distilled from. This page expresses that whole
> pipeline as **one cairn workspace** to prove the abstraction covers a real system end to end.
> That port has since **landed** (2026-07-04): brease-factory's `v2` branch is the first real
> workspace — note it evolved past this page's single-pipeline sketch into **one pipeline per track**
> (clone / redesign / reimagine), so treat this page as the historical worked mapping, not the
> as-built shape.
> Two things it references are **separate from cairn itself**: the *Brease CMS* (third-party content
> tooling the `brease=on` branch drives — not a cairn feature) and the vendor coding-agent CLIs the
> `agent:` steps call.

The mapping is 1:1: every mechanism the origin pipeline has today (all conditional steps, both gates,
the review loop, the CMS branch, deploy) has a home in the workspace below, and the fidelity table at
the end makes that explicit.

## `pipelines/brease-rebuild.yaml`

```yaml
pipeline: brease-rebuild
version: 1

params:
  url:    { type: string, required: true }
  mode:   { type: enum, values: [rebuild, redesign, reimagine], default: rebuild }
  pages:  { type: string, default: all }         # <n> | all — the scope gate always asks (headless → default)
  brease: { type: enum, values: ["on", "off"], default: "off" }   # quote! bare on/off are YAML booleans
  deploy: { type: enum, values: ["on", "off"], default: "on" }
  asset_budget: { type: int, default: 5 }
  variant: { type: string, default: "" }

dims:
  from: mode
  presets:
    rebuild:   { content: keep,    design: reproduce, brand: keep, routes: keep }
    redesign:  { content: keep,    design: redesign,  brand: keep, routes: keep }
    reimagine: { content: rewrite, design: redesign,  brand: new,  routes: restructure }

run_id: "{slug(params.url)}-{params.mode}-{date}{dash(params.variant)}"

artifacts:
  discovery:      { path: captures/discovery.json,       schema: schemas/discovery.json }
  selected-urls:  { path: captures/selected-urls.txt,    validator: validators/nonempty.py }
  site-map:       { path: captures/site-map.json,        schema: schemas/site-map.json, validator: validators/p0.py,
                    describe: "every captured page: url, type, ordered sections[], images[], nav+footer" }
  design-signals: { path: captures/design-signals.json,  schema: schemas/design-signals.json,
                    describe: "palette, fonts, spacing scale, imagery notes from the source site" }
  mode-plan:      { path: decisions/mode-plan.json,      schema: schemas/mode-plan.json, validator: validators/p1.py }
  strategy-brief: { path: decisions/strategy-brief.json, schema: schemas/strategy.json, validator: validators/p15.py,
                    describe: "personas, positioning, journey, per-page copy framework, rebrand brief, 301 map" }
  blueprints:     { path: "blueprints/**",               validator: validators/p2.py }
  design-md:      { path: blueprints/DESIGN.md,          validator: validators/design-md.py }
  asset-manifest: { path: assets/asset-manifest.json,    schema: schemas/asset-manifest.json }
  brease-context: { path: .brease/context.json,          validator: validators/brease-auth.py }
  brease-config:  { path: brease/brease.config.json,     schema: schemas/brease-config.json }
  content-map:    { path: brease/content-map.json,       schema: schemas/content-map.json, validator: validators/p3.py }
  frontend:       { path: "frontend/**",                 validator: validators/p4.py }
  art-review:     { path: "qa/art-review-r{cycle}.json", schema: schemas/art-review.json, validator: validators/p45.py }
  qa-report:      { path: qa/demo-report.json,           schema: schemas/qa-report.json, validator: validators/p5.py }
  deploy-report:  { path: deploy/deploy.json,            schema: schemas/deploy.json, validator: validators/p6.py }

guards:
  - name: no-screenshot-media                    # F18
    match: { tool: bash, command: "brease* createMedia*" }
    check: guards/f18.py
    enforce: [hook, shim, post]
    on_error: allow
  - name: wrong-cms-target
    when: params.brease == 'on'
    match: { tool: bash, command: "brease*" }
    check: guards/cms-target.py
    enforce: [hook, shim]
    on_error: deny                               # CMS mutation fails CLOSED

steps:
  # ---------- P0 CAPTURE: discover ▸ gate ▸ select ▸ capture ----------
  - step: discover
    agent: site-extractor
    args: { step: discover }
    produces: [discovery]
    timeout: 15m

  - gate: scope
    reads: [discovery]
    ask: "Which pages should we capture?"
    options:
      recommended: "nav-linked home/core/product pages"
      all: "everything discovered"
      core: "home + core pages only"
    default: all

  - step: select-urls                              # deterministic — no model needed
    run: "uv run python skills/crawl4ai/scripts/discover_urls.py --select {gate:scope} --pages {params.pages} --out {artifact:selected-urls}"
    needs: [discovery]
    produces: [selected-urls]

  - step: capture
    agent: site-extractor
    args: { step: capture }
    needs: [discovery, selected-urls]
    produces: [site-map, design-signals]
    timeout: 60m

  # ---------- P1 AUDIT · P1.5 STRATEGY (reimagine only) ----------
  - step: audit
    agent: site-auditor
    needs: [site-map, design-signals]
    produces: [mode-plan]

  - step: strategy
    when: dims.content == 'rewrite'
    agent: strategist
    needs: [mode-plan, site-map]
    produces: [strategy-brief]

  # ---------- P2 BLUEPRINT: concurrent pair ----------
  - parallel: blueprint
    steps:
      - step: architect
        agent: blueprint-architect
        needs: [mode-plan, site-map]             # + strategy-brief when it exists:
        needs_optional: [strategy-brief]
        produces: [blueprints]
      - step: design-author
        agent: design-director
        args: { job: author }
        needs: [mode-plan, design-signals]
        needs_optional: [strategy-brief]
        produces: [design-md]

  # ---------- P2.5 ASSETS (self-skips on no gap) ----------
  - step: assets
    agent: asset-generator
    args: { budget: "{params.asset_budget}" }
    needs: [blueprints, design-md, site-map]
    produces: [asset-manifest]
    skippable: true

  # ---------- P3 BREASE CMS (brease=on only; mutation gate first) ----------
  - step: brease-auth
    when: params.brease == 'on'
    manual: "Ensure the brease CLI is authenticated for THIS run: `cd {run_dir} && brease login && brease use` (target: {params.url})."
    produces: [brease-context]

  - step: model-cms
    when: params.brease == 'on'
    agent: modeler
    needs: [blueprints]
    produces: [brease-config]

  - gate: populate-approval
    when: params.brease == 'on'
    reads: [brease-config, brease-context]
    ask: "P3 will MUTATE the Brease CMS shown above. Proceed?"
    options: { "yes": "populate the CMS", "no": "halt here" }   # quote! bare yes/no are YAML booleans
    default: "no"                                 # headless NEVER auto-mutates a CMS

  - step: populate
    when: params.brease == 'on' && gates.populate-approval.choice == 'yes'
    agent: populator
    needs: [brease-config, blueprints, asset-manifest, brease-context]
    produces: [content-map]
    retry: { attempts: 0 }                        # CMS mutation: never blind-retry

  # ---------- P4 FRONTEND ----------
  - step: build
    agent: frontend-builder
    needs: [blueprints, design-md]
    needs_optional: [asset-manifest, content-map]
    produces: [frontend]
    timeout: 90m

  # ---------- P4.5 ART REVIEW (bounded loop; skipped when design=reproduce) ----------
  - loop: art-review
    when: dims.design != 'reproduce'
    min: 1
    max: { interactive: 3, headless: 2 }
    until: artifacts.art-review.verdict == 'approve'
    on_cap: continue
    body:
      - step: review
        agent: design-director
        args: { job: review, cycle: "{cycle}" }
        needs: [frontend, design-md]
        produces: [art-review]
        executor: claude                          # judgment stays on the strongest reviewer
      - step: revise
        agent: frontend-builder
        args: { revision: "on", cycle: "{cycle}" }
        needs: [art-review, design-md]
        produces: [frontend]
        unless: artifacts.art-review.verdict == 'approve'

  # ---------- P5 QA · P6 DEPLOY ----------
  - step: qa
    agent: qa-validator
    needs: [frontend, blueprints, design-md]
    produces: [qa-report]

  - step: deploy
    when: params.deploy == 'on' && artifacts.qa-report.verdict == 'GO'
    agent: deployer
    needs: [frontend, qa-report]
    produces: [deploy-report]
```

## `agents/` (all twelve, one file each — three shown)

```yaml
# agents/site-extractor.yaml
description: "Crawls the source site into structured captures/ via crawl4ai"
tier: balanced
effort: medium
skills: [brease-capture-site, crawl4ai]
tools: { allow: [read, write, edit, bash], bash: allowlist.yaml#capture }

# agents/design-director.yaml
description: "One mind two jobs: P2 DESIGN.md author · P4.5 section-by-section reviewer"
tier: reasoning
effort: high
skills: [brease-design-md, brease-frontend-design, brease-art-review]
tools: { allow: [read, write, bash], bash: allowlist.yaml#readonly-plus-screenshot }

# agents/frontend-builder.yaml
description: "Scaffolds+builds the Next.js 16 demo, one component per section key"
tier: balanced
effort: medium
escalate: { when: "dims.design != 'reproduce'", tier: reasoning }   # the old '*' rule
skills: [brease-build-frontend, brease-frontend-next, web-section-design]
tools: { allow: [read, write, edit, bash], bash: allowlist.yaml#node-build }
```

## Runs

```console
# interactive redesign on Codex, review pinned to Claude (mixed fleet)
$ cairn run brease-rebuild --param url=https://acme.com --param mode=redesign --executor codex
  ▸ gate scope: [r]ecommended / [a]ll / [c]ore ?  r
  …
  ✔ done · runs/acme-redesign-20260702 · 9 steps · art-review approved r2 · qa GO · deployed

# reimagine, fully headless (strategy fires; gates use defaults; populate-approval defaults NO)
$ cairn run brease-rebuild --param url=https://acme.com --param mode=reimagine --headless

# resume after a P4 validator halt (fix was a blueprint edit)
$ cairn resume runs/acme-reimagine-20260702
  ✔ discover…design-author  (valid, skipped) · ▶ build (retry 1, validator reasons in envelope)

# credential-free batch to blueprints, 8 sites in parallel
$ cairn batch brease-rebuild --params-file sites.jsonl -j 8 --to blueprint --gate scope=all
```

## Fidelity map — every current mechanism has a home

| Today (Claude-Code implementation) | In cairn |
|---|---|
| `/brease-rebuild <url> mode=… pages=… run=..P2` | `cairn run brease-rebuild --param … --to blueprint` |
| Orchestrator skill + Workflow JS batch | kernel walker + `cairn batch` (one implementation) |
| 12 `.claude/agents/*.md` (model/effort/tools/skills) | `agents/*.yaml` (tier/effort/tools/skills) |
| `*`-rule opus escalation in redesign/reimagine | `escalate: {when: dims.design != 'reproduce'}` |
| Mode → dimensions preset (`mode-dimensions.md`) | `dims.presets` table in the pipeline |
| P0 discover→gate→capture split | `discover` step · `scope` gate · `select-urls` run-step · `capture` step |
| P1.5 reimagine-only strategist | `strategy` step, `when: dims.content == 'rewrite'` |
| P2 architect ∥ design-director | `parallel: blueprint` |
| P2.5 self-skip on no media gap | `assets` step, `skippable: true`, STEP `status: skipped` |
| P3 requires auth + explicit go-ahead, mutates CMS | `brease-auth` manual step · `populate-approval` gate (headless default **no**) |
| P4.5 bounded review⇄revise, no-first-pass-sign-off | `loop: art-review` (min 1, max 3/2); first-pass rule stays in `validators/p45.py` |
| P5 GO/NO-GO gating P6 | `deploy.when: artifacts.qa-report.verdict == 'GO'` |
| `validate-artifact.py` hard gate per phase | per-artifact `validator:` — same scripts, decomposed |
| F18 + wrong-CMS PreToolUse guards | `guards:` block — hook+shim+post, wrong-CMS fails **closed** |
| `run.json` / `progress.jsonl` / `.active-run` | `run.json` (pinned schema) / `trail.jsonl` / per-process env (`CAIRN_RUN_DIR`) |
| Run-dir isolation + wrong-run tripwire | kernel-owned cwd/env + envelope tripwire line |
| SubagentStart brief / SubagentStop log+gate | envelope block 4 (trail context) / walker post-step (validate+trail) |
| CLAUDE.md doctrine | `prompts/DOCTRINE.md` → envelope block 5 + `render_workspace` (AGENTS.md etc.) |
| `learnings.jsonl` + self-improve loop | STEP `learnings[]` → trail `learn` events → `cairn learnings` aggregation *(aggregate verb LIVE; the curate→promote `self-improve.yaml` ships in the workspace scaffold — TOOLING §7)* |

Nothing in the current system failed to land *in the design* — every mechanism has a home above.
Build status is a separate axis: this pipeline, its twelve agents, both gates, the parallel pair,
the art-review loop, the conditional chains, and the guard block all **plan and stub-run today**
(they are the C1 planner fixture, byte-for-byte). The ops verbs those rows lean on — `cairn batch`
and the `cairn learnings` aggregation — are now **built and tested (LIVE)**, and the `learnings`
promote pipeline ships as scaffold furniture (`self-improve.yaml` — TOOLING §7); the executors
behind the `agent:` steps are live (C2–C5 done), and the brease-factory workspace migration has
**landed** (2026-07-04 — as one pipeline per track rather than this page's single pipeline); what
still waits is the first live run + parity benchmark, and the `brease=on` CMS branch remains gated
on the separate Brease CLI. Two things got *stronger* in translation: the batch guard is no longer fail-open
(per-process env replaces the global pointer), and CMS population now fails closed and defaults to
"no" headlessly.
