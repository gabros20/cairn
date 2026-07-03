# cairn — Distribution

The operational design behind README §Packaging & embedding: what the package actually is, how
versions and compatibility work, what scaffolding ships, how a machine is onboarded, and how a
coding agent operates cairn. Philosophy lives in the README; this is the mechanics.

---

## 1. Package anatomy

```
designatives/cairn                     # the tool's own repo
├── pyproject.toml
│     name = "cairn"                   # PyPI fallback if squatted: "cairn-pipelines"
│     requires-python = ">=3.11"
│     dependencies = ["pyyaml", "jsonschema"]        # the FULL list — additions need a design reason
│     [project.scripts]  cairn = "cairn.cli:main"
│     [project.entry-points."cairn.executors"]
│       shell  = "cairn.executors.shell:ShellExecutor"
│       stub   = "cairn.executors.stub:StubExecutor"     # the offline test executor (TESTING §5)
│       claude = "cairn.executors.claude:ClaudeExecutor"
│       codex  = "cairn.executors.codex:CodexExecutor"
│       grok   = "cairn.executors.grok:GrokExecutor"
├── cairn/                             # kernel (ARCHITECTURE §1) + executors + py.typed
├── templates/workspace/               # what `cairn new workspace` instantiates (§4);
│                                      #   force-included into the wheel as cairn/_templates/workspace
└── tests/                             # the C1 synthetic suite — the release gate
```

Build backend is **hatchling**; the wheel ships `packages = ["cairn"]` and force-includes
`templates/workspace` → `cairn/_templates/workspace` so `cairn new workspace` works from an
installed copy (`newkit.templates_dir()` resolves that path first). This is the standalone repo
today (`~/Documents/Work/Projects/cairn`, run in place with `uv run cairn …`); C7 packaging just
adds the tag + wheel, no tree moves.

## 2. Install channels

A note on lineage, so the phases below read correctly: cairn was **designed inside brease-factory**
(the pipeline it was distilled from, and its eventual first workspace — README §Lineage), but it has
been **built as its own standalone repo from day one** (`~/Documents/Work/Projects/cairn`). It has
never lived *inside* brease-factory as a subtree. So "extraction" (C7) does **not** mean moving a tree
out of another repo — it means **packaging and tagging** this repo: cutting `v0.1.0`, building the
wheel, and installing it as a tool instead of running it in place.

| Phase | Channel | Command |
|---|---|---|
| Development (pre-tag) | in-repo | `uv run cairn …` (this repo, in place) |
| Internal (C7 packaging) | git tag | `uv tool install git+https://github.com/designatives/cairn@v0.1.0` |
| Public (if open-sourced) | PyPI | `uv tool install cairn` · zero-install `uvx cairn …` |

Upgrades: `uv tool upgrade cairn` (or install a newer tag). Downgrade = install the older tag —
safe because workspaces pin (§3) and runs record versions.

## 3. Versioning & compatibility — three surfaces, one policy

SemVer on the tool, with **three declared compatibility surfaces**:

| Surface | Declared by | Checked | Breaking ⇒ |
|---|---|---|---|
| **Pipeline schema** | `version: 1` in each pipeline file | at plan: kernel lists supported versions; unknown ⇒ exit 2 with migration pointer | new schema version + kernel keeps reading N−1 for one minor series |
| **Executor protocol** | third-party executors declare `cairn_api = 1` | at load: mismatch ⇒ executor skipped with warning | major bump of the tool |
| **Run-dir format** | `run.json.cairn_version` | at resume: same-minor ⇒ silent; cross-minor ⇒ warn; cross-major ⇒ refuse without `--force` | major bump + a `cairn migrate-run` shim only if ever actually needed |

Workspace side: `cairn.toml → requires = ">=0.1,<0.2"`, enforced at plan time (refuse, print the
installed vs required range). `run.json` records the exact tool version, executor versions, and
pipeline content-hash — so any old run is diagnosable regardless of what's installed today.

*Status: two surfaces are enforced today. Run-dir **pipeline-hash** drift — `cairn resume` (and
`run --idempotent`'s resume path) refuses a changed pipeline without `--force`. And the workspace
**`requires`-pin** — `cairn plan` (via `config.check_requires`) now **refuses to plan** when the
installed cairn falls outside `cairn.toml: requires = …`, naming both the required range and the
installed version. cairn's own version is **0.1.0** (single-sourced from `cairn/__init__.py`); the
`v0.1.0` git tag is being cut this wave. The remaining schema-version and executor-protocol gates
stay parsed-but-not-yet-enforced (IMPLEMENTATION-PLAN).*

Release discipline: a git tag is releasable iff the synthetic suite is green and `cairn plan`
passes over the example + brease workspaces. CHANGELOG entry per tag; migration notes on any
schema-surface change. Nothing more ceremonial than that.

## 4. The workspace scaffold — `cairn new workspace <name>`

Instantiates `templates/workspace/`, which must satisfy one hard requirement: **`cairn run hello`
works immediately, offline, with zero auth** — the day-0 experience uses only `run:` steps.

```
<name>/
├── cairn.toml               # default_executor = "claude"; tiers commented; one [tools] example
├── pipelines/hello.yaml     # run: steps + one gate (default preset) — plans AND runs offline;
│                            # a commented agent: step shows the first real upgrade
├── agents/assistant.yaml    # minimal agent, ready for the uncommented step
├── skills/cairn-operator/   # the operator skill (§6) — ships in every workspace
├── schemas/step-return.json # + greeting.json — the hello pipeline's artifact schema
├── validators/nonempty.py   # the starter validator (maturation ladder rung 0)
├── prompts/DOCTRINE.md      # stub: isolation invariant + artifact-authority rule
├── allowlist.yaml           # empty fragments with comments
├── .gitignore               # runs/
└── README.md                # the TOOLING-AND-GROWTH maturation ladder, condensed
```

A separate GitHub template repo is optional sugar: it is exactly this scaffold committed — never a
kernel copy.

## 5. Machine onboarding

```console
$ uv tool install git+https://github.com/designatives/cairn@v0.1.0
$ cd my-workspace && cairn doctor
  ✔ cairn 0.1.0
  ✔ workspace lint  3 pipelines plan green
  ✔ executor claude   healthy
  ✗ executor codex    codex not found → npm i -g @openai/codex          # only when --executor codex
  ✔ tool crawl4ai
  ✗ tool vercel       `vercel whoami` failed → pnpm add -g vercel && vercel login (needed by: deploy)
  ✔ secret BREASE_TOKEN    present
  ✔ guard runner    cairn.kernel.guards imports
```

Real order today: version · workspace lint · in-scope executors · `[tools]` · `[secrets]` presence ·
guard-runner import; `--probe-hooks` adds a per-executor `hook probe` line — it spawns a canary and
classifies fires+blocks / fires-not-blocks / no-fire / inconclusive (on the dev machine both claude
and codex report fires+blocks → hook-primary). Rules: doctor
checks the *default* executor + any named via `--executor`, and only a lint error or a broken
in-scope executor fails its exit — `[tools]`/`[secrets]`/guard-runner problems are **warnings**
(a missing vercel prints its `needed_by` scope but blocks nothing). Per-run auth (e.g. `brease
login` into a run dir) is not doctor's job — it's a `manual:` step in the pipeline (TOOLING §2).

*Status: the per-executor line currently reports `healthy`/its first finding; the richer
`(version, auth ok, hooks: blocking)` detail arrives as the live executors land (claude + codex done,
grok is C5), and the `--probe-hooks` hook line has shipped (C4). The
`requires` pin itself is now enforced — at **plan** time (`cairn plan` refuses an out-of-range
install, §3); surfacing it as a dedicated `satisfies requires …` line in `doctor` is the remaining
cosmetic piece.*

## 6. The operator skill — coding agents driving cairn

Ships in the scaffold as `skills/cairn-operator/SKILL.md`, teaching any interactive coding agent
(Claude Code, Codex…) the operator pattern. Its whole contract:

1. **Plan before run.** `cairn plan <pipeline> --json`; relay diagnostics; never "fix" by editing
   `runs/`.
2. **Run headless, watch the trail.** `cairn run … --headless` in background; poll
   `cairn trail <run> --json` for status, not the process stdout.
3. **Exit 6 = a gate is waiting.** Read the pending gate from trail, ask the human through your
   own UI, answer with `cairn gate <run> <name>=<choice>`, then `cairn resume <run>`.
4. **Exit 3 = a validator failed.** Read `validator_reasons` from the trail, inspect the named
   artifact and `logs/<step>.log`, propose the workspace fix, resume after approval.
5. **Exit 4/5 = machine trouble.** `cairn doctor`, relay findings, resume when fixed.
6. **Never** hand-edit run dirs, answer a gate the human didn't decide, or bypass a halt.

This file is the *entire* integration between cairn and every conversational agent — which is the
point: the boundary is a CLI + exit codes + JSON, so the "integration" is documentation.

## 7. Sharing between workspaces — named non-feature (v1)

No skill/pipeline registry, no remote imports, no workspace inheritance. Sharing = git: copy the
files, or submodule a skills directory if two workspaces genuinely co-evolve. Registry-shaped
machinery is the canonical framework-bloat trapdoor; revisit only when ≥3 real workspaces exist
and copying demonstrably hurts. (The executor entry-point surface already covers the one thing
that must be shared as code.)

## 8. Open questions deferred, deliberately

- **License / open-sourcing** — decide at C7; nothing before it depends on the answer.
- **PyPI name** — check `cairn` at publish time; fallback `cairn-pipelines` reserved in §1.
- **MCP server plugin** (trail resource + run/gate/resume tools) — only if the operator skill
  proves insufficient in practice; it hasn't been given the chance to yet.
