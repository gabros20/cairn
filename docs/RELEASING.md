# cairn — Releasing

How a cairn release happens, end to end. Releases are automated by
[python-semantic-release](https://python-semantic-release.readthedocs.io/) (PSR) driven from
`.github/workflows/release.yml`; you never bump a version or write a changelog entry by hand.

> **First release?** Jump to [Activation checklist](#activation-checklist) — a handful of one-time
> setup steps must be done before the first publish will work.

---

## How versions are decided

PSR reads every [Conventional Commit](https://www.conventionalcommits.org/) since the last `v*` tag
and picks the next version from the commit types:

| Commit | Example | Version bump |
|---|---|---|
| `fix:` | `fix(cli): honest preflight docstring` | patch — `0.1.0 → 0.1.1` |
| `feat:` | `feat(release): semantic-release workflow` | minor — `0.1.0 → 0.2.0` |
| `BREAKING CHANGE:` (footer) or `feat!:` / `fix!:` | `feat!: drop the v1 pipeline schema` | major — `0.1.0 → 1.0.0` |
| `docs:` / `chore:` / `style:` / `test:` / `refactor:` | `docs: refresh README` | none on their own |

If no commit since the last tag warrants a bump, **no release is cut** — the workflow runs and
exits cleanly. This is why enabling the `push: main` trigger (commented out in the workflow) is
safe: routine `docs:`/`chore:` pushes never release.

The version is single-sourced from `cairn/__init__.py:__version__`. PSR stamps that variable
(`[tool.semantic_release] version_variables`), and `pyproject.toml` reads it dynamically via
hatchling — so there is only one place the version lives.

## What a release does

Dispatching the workflow runs two jobs:

1. **`release`** — PSR evaluates the commits, and if a bump is warranted it:
   - stamps the new version into `cairn/__init__.py`,
   - inserts a new section into `CHANGELOG.md` above the `<!-- version list -->` marker,
   - runs the `build_command` (installs uv, re-locks `uv.lock` at the new version, builds the
     sdist + wheel),
   - commits those changes, tags them `vX.Y.Z`, and pushes,
   - creates the GitHub Release and uploads the built distributions to it.
2. **`publish`** — only if the `release` job actually cut a version, downloads the built
   distributions and uploads them to PyPI via **Trusted Publishing** (OIDC — no API token).

## Cutting a release

1. Land your changes on `main` as Conventional Commits (the repo already uses this convention).
2. Go to **Actions → release → Run workflow**, pick `main`, and dispatch it.
   - Or, from a checkout with the `gh` CLI: `gh workflow run release.yml --ref main`.
3. Watch the run. On success you'll see a new `vX.Y.Z` tag, a GitHub Release with the sdist + wheel
   attached, an updated `CHANGELOG.md`, and the package live on PyPI.

There is nothing to run locally. To preview what *would* be released without doing it, from a full
checkout: `uvx python-semantic-release version --print` (prints the next version) — this never
writes, commits, or tags. Note it needs an `origin` remote to resolve the release branch, so it
only works once the GitHub repo exists and is set as `origin` (see the activation checklist).

## Activation checklist

These are one-time steps before the **first** publish. Until they are done, the workflow either
fails or publishes nowhere.

- [x] **Create the GitHub repo** and push `main` plus the existing `v0.1.0` tag —
      done: [`gabros20/cairn`](https://github.com/gabros20/cairn) (public).
- [x] **Fill `[project.urls]` in `pyproject.toml`** — done: all URLs point at `gabros20/cairn`.
- [ ] **Configure the PyPI Trusted Publisher** for the `cairn-pipelines` project. On PyPI, add a
      *pending* publisher (the project does not exist yet) under **Your projects → Publishing**:
   - PyPI Project Name: `cairn-pipelines`
   - Owner: `gabros20` · Repository: `cairn`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
- [x] **Create the `pypi` environment** in the GitHub repo — done (no protection rules yet; add
      required reviewers later if you want a manual approval gate before anything reaches PyPI).
- [x] **Confirm Actions can write** — done: default workflow permissions set to read/write.
- [x] **Allow the release job past branch protection** — N/A: `main` is not protected. If
      protection is added later, exempt the repo's GitHub Actions bot in the bypass list (or run
      releases from an unprotected release branch), else the `release` job fails at the push step.
- [ ] **Dispatch the workflow** (see [Cutting a release](#cutting-a-release)). The first run
      releases whatever the commits since `v0.1.0` warrant; if that's nothing yet, make a
      `feat:`/`fix:` commit first.

## The name situation

The import package and the console script are both `cairn`. The **PyPI distribution** is
`cairn-pipelines`, because the bare `cairn` name on PyPI is taken by an unrelated tool
(`ejfitzgerald/cairn`). Practical consequences:

- Install with `uv tool install cairn-pipelines` (or `uvx --from cairn-pipelines cairn …`); the
  command you run is still `cairn`.
- **Do not install both `cairn` and `cairn-pipelines`** into the same environment — they collide on
  the `cairn` import package and the `cairn` console script.
- Pre-publish, installing from git still works and is name-agnostic:
  `uv tool install git+https://github.com/gabros20/cairn@v0.1.0`.

## Reference

- Config lives in `pyproject.toml` under `[tool.semantic_release]`, verified against PSR 10.5.3.
- Pinned action refs (bump together when upgrading PSR):
  `python-semantic-release/python-semantic-release@v10.5.3`,
  `python-semantic-release/publish-action@v10.5.3`,
  `pypa/gh-action-pypi-publish@v1.14.0`.
