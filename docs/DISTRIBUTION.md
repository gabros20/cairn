# cairn — Distribution

The operational design behind README §Packaging & embedding: what the package actually is, how
versions and compatibility work, what scaffolding ships, how a machine is onboarded, and how a
coding agent operates cairn. Philosophy lives in the README; this is the mechanics.

---

## 1. Package anatomy

```
cairn                                  # the tool's own repo
├── pyproject.toml
│     name            = "cairn-pipelines"     # PyPI DISTRIBUTION name (§ name situation below)
│     dynamic         = ["version"]           # hatch reads __version__ from cairn/__init__.py
│     requires-python = ">=3.11"
│     license         = "MIT"                 # LICENSE + license-files
│     dependencies    = ["pyyaml", "jsonschema"]   # the FULL list — additions need a design reason
│     [project.scripts]  cairn = "cairn.cli:main"  # the console script stays `cairn`
│     [project.entry-points."cairn.executors"]
│       shell  = "cairn.executors.shell:ShellExecutor"
│       stub   = "cairn.executors.stub:StubExecutor"     # the offline test executor (TESTING §5)
│       claude = "cairn.executors.claude:ClaudeExecutor"
│       codex  = "cairn.executors.codex:CodexExecutor"
│       grok   = "cairn.executors.grok:GrokExecutor"
├── cairn/                             # kernel (ARCHITECTURE §1) + executors + __init__.py:__version__
├── templates/workspace/               # what `cairn new workspace` instantiates (§4);
│                                      #   force-included into the wheel as cairn/_templates/workspace
├── .github/workflows/{ci.yml,release.yml}  # the release gate (§3) + the release automation (§3.1)
└── tests/                             # the synthetic suite — the release gate's core
```

**One tool, three names, one collision.** The import package and the console script are both
`cairn`; the **PyPI distribution** is `cairn-pipelines`, because the bare `cairn` name on PyPI is
already taken by an unrelated tool. So you `uv tool install cairn-pipelines` but the command you run
is still `cairn` — and you must never install both `cairn` and `cairn-pipelines` into one
environment, since they collide on the `cairn` import package and script. The full rationale and the
pending-publisher setup live in [RELEASING.md](RELEASING.md) *(§ The name situation)*.

Build backend is **hatchling**. The version is **single-sourced** from `cairn/__init__.py:__version__`
— `pyproject` declares `dynamic = ["version"]` and `[tool.hatch.version]` reads that variable, so
there is exactly one place the version lives (and PSR stamps it, §3.1). The wheel ships
`packages = ["cairn"]` and **force-includes** `templates/workspace` → `cairn/_templates/workspace`
so `cairn new workspace` works from an installed copy (`newkit.templates_dir()` resolves that path
first). cairn is its own standalone repo, run in place with `uv run cairn …` during development;
packaging adds the tag + wheel, no tree moves.

## 2. Install channels

A note on lineage, so the channels below read correctly: cairn was **distilled from an internal
Claude-Code pipeline** (the origin system it generalizes, and its eventual first workspace — README
§Lineage), but it has been **built as its own standalone repo from day one**. It has never lived
*inside* that pipeline as a subtree, so packaging is not a tree move out of another repo — it is
**building and tagging** this repo: cutting a `vX.Y.Z` tag, building the wheel, and installing it as
a tool instead of running it in place.

| Channel | When | Command |
|---|---|---|
| in-repo | development | `uv run cairn …` (this repo, in place) |
| git tag | pre-publish / private | `uv tool install git+https://github.com/gabros20/cairn@v0.1.0` |
| PyPI | published | `uv tool install cairn-pipelines` · zero-install `uvx --from cairn-pipelines cairn …` |

The `v0.1.0` tag is cut; the PyPI channel activates once the one-time publisher setup in
[RELEASING.md](RELEASING.md) *(§ Activation checklist)* is done. Upgrades: `uv tool upgrade
cairn-pipelines` (or install a newer tag). Downgrade = install the older tag — safe because
workspaces pin (§3) and runs record versions.

## 3. Versioning & compatibility — three surfaces, one policy

SemVer on the tool, with **three declared compatibility surfaces**:

| Surface | Declared by | Checked | Breaking ⇒ |
|---|---|---|---|
| **Pipeline schema** | `version: 1` in each pipeline file | at plan: kernel lists supported versions; unknown ⇒ exit 2 with migration pointer | new schema version + kernel keeps reading N−1 for one minor series |
| **Executor protocol** | third-party executors declare `cairn_api = 1` | at load: mismatch ⇒ executor skipped with warning | major bump of the tool |
| **Run-dir format** | `run.json.cairn_version` | at resume: same-minor ⇒ silent; cross-minor ⇒ warn; cross-major ⇒ refuse without `--force`; a forced resume re-pins `cairn_version`/`pipeline_hash` to the present so the consent isn't re-paid every resume | major bump + a `cairn migrate-run` shim only if ever actually needed |

Workspace side: `cairn.toml → requires = ">=0.1,<0.2"`, enforced at plan time (refuse, print the
installed vs required range). `run.json` records the exact tool version, executor versions, and
pipeline content-hash — so any old run is diagnosable regardless of what's installed today.

*Status: three gates are enforced today. Run-dir **pipeline-hash** drift — `cairn resume` (and
`run --idempotent`'s resume path) refuses a changed pipeline without `--force`. The workspace
**`requires`-pin** — `cairn plan` (via `config.check_requires`) **refuses to plan** when the
installed cairn falls outside `cairn.toml: requires = …`, naming both the required range and the
installed version. And the **run-dir format version gate** — every resume entrance (including
`run`'s resume paths) compares `run.json.cairn_version` to the installed version exactly per the
table: same-minor silent, cross-minor warn, cross-major refuse without `--force`; an unreadable
`run.json` fails loud (`CONFIG`), never silently proceeds. cairn's own version is **0.1.0**
(single-sourced from `cairn/__init__.py`); the `v0.1.0` git tag is cut. The schema-version and
executor-protocol gates stay parsed-but-not-yet-enforced (IMPLEMENTATION-PLAN).*

## 3.1 How a release is cut — conventional commits, no manual bump

Releases are **automated** by [python-semantic-release](https://python-semantic-release.readthedocs.io/)
(PSR), configured in `pyproject.toml` `[tool.semantic_release]` and driven by
`.github/workflows/release.yml`. Nobody edits a version number or writes a changelog entry by hand:

- **The version comes from the commits.** PSR reads every [Conventional
  Commit](https://www.conventionalcommits.org/) since the last `v*` tag — `fix:` → patch, `feat:`
  → minor, a `BREAKING CHANGE:` footer (or `feat!:`) → major — and picks the next version. If
  nothing since the last tag warrants a bump, **no release is cut**.
- **What a dispatch does.** The workflow is `workflow_dispatch` (manual-first). Its `release` job
  stamps the new version into `cairn/__init__.py`, prepends a `CHANGELOG.md` section above the
  `<!-- version list -->` marker, re-locks `uv.lock`, builds the sdist + wheel, commits, tags
  `vX.Y.Z`, and creates the GitHub Release; a second `publish` job — only when a version was
  actually cut — uploads the dist to PyPI via **Trusted Publishing** (OIDC, no API token).
- **The step-by-step and the one-time activation** (create the repo, fill `[project.urls]`,
  register the PyPI pending publisher, create the `pypi` GitHub environment) live in
  [RELEASING.md](RELEASING.md) — this section is the *shape*, that doc is the *how-to*.

Migration notes still ride along on any schema-surface change; nothing more ceremonial than that.

## 3.2 The release gate — `.github/workflows/ci.yml`

CI is the gate a release rides on. On every push to `main` and every pull request, `ci.yml` runs
four jobs: the **unit suite** (`uv run pytest tests/unit`), a **plan** job that scaffolds a fresh
workspace with `cairn new` and runs `cairn plan` over every shipped pipeline (plus the live
smoke workspaces, then replays each offline through the `stub` executor), a **doctor smoke** that
asserts `cairn doctor` on a bare machine reports findings without ever crashing (accepts exit 0 or
2), and a **wheel install smoke** on Linux **and** macOS that builds the wheel, `uv tool install`s
it, and runs `cairn new` + `cairn run hello --headless` from the installed copy — proving the
`templates/workspace` force-include (§1) actually shipped inside the wheel and the day-0 path works
end to end.

## 4. The workspace scaffold — `cairn new workspace <name>`

Instantiates `templates/workspace/`, which must satisfy one hard requirement: **`cairn run hello`
works immediately, offline, with zero auth** — the day-0 experience uses only `run:` steps.

```
<name>/
├── cairn.toml               # default_executor = "claude"; [executors.claude] tiers active;
│                            # [tools.gh] scoped needed_by=["open-pr"] — tool enforcement, dogfooded
├── pipelines/hello.yaml     # run: steps + one gate (default preset) — plans AND runs offline;
│                            # a commented agent: step shows the first real upgrade
├── pipelines/self-improve.yaml  # the learning loop's curate→promote (TOOLING §7): aggregate →
│                            # curate → approve gate (headless default "no") → open-pr
├── agents/assistant.yaml    # minimal agent, ready for the uncommented step
├── agents/curator.yaml      # the self-improve judge (tier-routed, executor-agnostic)
├── skills/cairn-operator/   # the operator skill (§6) — ships in every workspace
├── skills/self-improve-curator/  # the curation doctrine — the workspace's policy seam
├── schemas/step-return.json # + greeting.json + self-improve-proposals.json
├── validators/nonempty.py   # + self-improve-proposals.py (targets stay in-workspace, unique ids)
├── scripts/self-improve-open-pr.py  # worktree-isolated apply → branch → PR via gh
├── prompts/DOCTRINE.md      # stub: isolation invariant + artifact-authority rule
├── tests/                   # matrix + fixtures + stubs — `cairn test` green out of the box,
│                            # incl. the recorded headless walk proving the gate refuses ("no")
├── allowlist.yaml           # empty fragments with comments
├── .gitignore               # runs/
└── README.md                # the TOOLING-AND-GROWTH maturation ladder, condensed
```

Retrofit path for a workspace that predates the furniture: `cairn new pipeline self-improve`
wires the same files into an existing workspace, append-only — it adds the test-matrix row and
never clobbers customized companion files (no new CLI surface).

A separate GitHub template repo is optional sugar: it is exactly this scaffold committed — never a
kernel copy.

## 5. Machine onboarding

```console
$ uv tool install git+https://github.com/gabros20/cairn@v0.1.0
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
classifies fires+blocks / fires-not-blocks / no-fire / inconclusive (on the dev machine claude,
codex, and grok all report fires+blocks → hook-primary). Rules: doctor
checks the *default* executor + any named via `--executor`, and only a lint error or a broken
in-scope executor fails its exit — `[tools]`/`[secrets]`/guard-runner problems are **warnings**
(a missing vercel prints its `needed_by` scope but blocks nothing — though `cairn run`/`resume`
now hard-stop before minting when a *scoped* tool's check fails, TOOLING §2). Per-run auth (e.g.
`brease login` into a run dir) is not doctor's job — it's a `manual:` step in the pipeline
(TOOLING §2).

*Status: the per-executor line currently reports `healthy`/its first finding; the richer
`(version, auth ok, hooks: blocking)` detail is still cosmetic follow-up now that the three
original vendor executors (claude, codex, grok) are live — five more (cursor, opencode, hermes,
kimi, agy) are adapter-complete with a real-CLI smoke pending — and the `--probe-hooks` hook line
has shipped. The
`requires` pin is enforced at **plan** time (`cairn plan` refuses an out-of-range install, §3),
and doctor now prints its own `requires` satisfied/not-satisfied line whenever the workspace pins
one.*

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

## 8. Settled, and still open

Settled since this doc was first drafted: the **license** is MIT (`LICENSE` + `license` in
`pyproject`), and the **PyPI name** is `cairn-pipelines` (the bare `cairn` was taken — §1, RELEASING).

Still deliberately open:

- **MCP server plugin** (trail resource + run/gate/resume tools) — only if the operator skill
  proves insufficient in practice; it hasn't been given the chance to yet.
