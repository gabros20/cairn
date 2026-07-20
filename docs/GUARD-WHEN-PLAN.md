# cairn — Runtime `when` on Guards: Design & Update Plan (C9)

**Status:** done (landed 2026-07-15) · **Finding:** codex-F12 / C9 (was deferred in
`IMPLEMENTATION-PLAN.md`, now marked done there) · **Author:** hardening review follow-up,
2026-07-15 · **Companion to:** `HARDENING-PLAN.md`, `ARCHITECTURE.md §4`, `SECURITY.md §2`.

This document specifies how to correctly honor a **runtime** `when:` condition on a `guards:` entry,
which today is silently dropped so the guard over-applies. It is written to be reviewed *before*
implementation because it refactors the W3a-hardened guard-enforcement path.

---

## 1. The problem, precisely

A `guards:` entry may carry a `when:` predicate. cairn already splits these at plan time
(`plan.py::_expand_condition`, `_static_check_paths`):

- **Plan-time `when`** — references only `params`/`dims`. The planner *settles* it now: an inactive
  guard is **dropped entirely**. ✔ Correct today.
- **Runtime `when`** — references `gates`/`artifacts`/`run`/`cycle` (unknowable at plan time). It is
  kept as a live `Expr` on `GuardDecl.when` (`plan.py:154`). ✘ **This is the bug.**

Guards are installed **once per run** (`walk.py::_install_guards` → `cli.py::_wrap_guards::build_shims`
for the shim layer, `claude.py::install_guards` for the hook layer). The install writes a **signed
manifest** (W3a: HMAC over the guard set + each check-script hash, stored outside the run dir at
`gatekeys.guard_manifest_path`). The shim/hook decision runs in a **subprocess** (`guards.py::_run_chain`
via `--shim-check`/`--hook-check`) that reads that manifest.

When the manifest is reloaded in the subprocess, `guards.py::_load_manifest_guard` sets **`when=None`**
(`guards.py:479`) — the runtime condition is not serialized and the subprocess has no evaluation
context. **Consequence:** a runtime-conditional guard runs its check on *every* matching invocation,
regardless of whether its `when` currently holds.

### 1.1 Severity — it fails *safe*

The dropped condition can only cause a guard to be active when it should be inactive → an **incorrect
denial** or an extra check run (over-strict). It can **never** wrongly *deactivate* a guard (a `when`
that should activate it is simply ignored → the guard stays active). So this is a **correctness /
availability** defect on the rarely-used runtime-`when`-on-a-guard shape, **not** a containment
weakening. That is why it was deferred rather than rushed into the hardened path at the end of the
hardening run.

### 1.2 Concrete failure

```yaml
guards:
  - name: no-deploy-until-approved
    match_command: "deploy *"
    when: "gates.approve.choice == 'yes'"   # guard ACTIVE only after approval  ← runtime
    check: guards/deny.sh
    enforce: [shim]
```

Intended: block `deploy` **until** the `approve` gate says yes; once approved, the guard deactivates and
`deploy` is allowed. Today: `when` is dropped, so `no-deploy-until-approved` runs its check on *every*
`deploy` — the command is blocked **even after** approval. The human "yes" is ignored (over-block).

---

## 2. Where `when` can be evaluated — the load-bearing decision

There are two candidate evaluation sites, and they are **not** security-equivalent.

### Option A — evaluate in the subprocess (`_run_chain`)
Serialize the `when` source into the signed manifest; `_run_chain` re-parses and evaluates it against
on-disk state.
- **Inputs it would read:** `params`/`dims` from `run.json` — **agent-writable** (it lives in the run
  dir); `gates` (would need to replicate `read_verified_choice`); `artifacts` (would need to replicate
  containment/resolution).
- **Problems:** (1) **less secure** — an agent can edit `run.json` to flip a `params`/`dims` `when` and
  deactivate a guard; (2) **duplicates** the walker's gate-verification + artifact-resolution logic into
  the *security-critical hardened path*, widening attack surface and risking divergence from the
  authoritative resolver.

### Option B — evaluate in the walker *(chosen)*
The walker already evaluates runtime `when` for **steps/gates/loops** via
`walk.py::_passes_when(when, cycle, node_id)` → `_eval` → `_resolver` (`walk.py:934-963`), resolving:
- `params`/`dims` → **`self.plan.params` / `self.plan.dims`** — trusted, held in memory, not re-read
  from the agent-writable `run.json` (the exception is the `run` root, which loads `run.json`; see §7).
- `gates` → `_resolve_gate_ref` → **W2-verified** `read_verified_choice`.
- `artifacts` → `_resolve_artifact_ref` → **W6-contained** paths.
- `cycle` → the loop binding.

`GuardDecl.when` on `self.plan.guards` is a **live `Expr`** (only the *manifest reload* nulls it). So the
walker can call `self._passes_when(guard.when, cycle, guard.name)` with **zero new machinery** and
against **trusted state the agent cannot tamper**.

### Decision: **Option B.**
Evaluate in the walker; keep the subprocess dumb. This is both **simpler** (reuses tested code) and
**more secure** (trusted inputs, no new logic in the hardened path). The subprocess (`_run_chain`) is
**unchanged** — it receives a manifest that has already been `when`-filtered, so an inactive guard is
simply absent → pass-through. `_load_manifest_guard`'s `when=None` becomes *correct by construction*
(the manifest legitimately never carries `when`).

---

## 3. The mechanism (Option B in detail)

### 3.1 Split "install" (static, once) from "manifest" (dynamic, per-invocation)

Today the manifest write is *coupled* to the once-per-run install. We decouple them:

| Concern | When | Where (today → after) |
|---|---|---|
| Shim **scripts** on PATH (static: they just call `--shim-check`) | once per run | `build_shims` (unchanged) |
| Hook **settings.json** `PreToolUse` entry (static: calls `--hook-check`) | once per run | `install_guards` (unchanged) |
| Signed **manifest** content (the *active* guard set) | **per invocation** | move OUT of install → the walker writes it per step |

The static scripts/settings are installed once and cover **every guard's binary** (so the shim is on
PATH / the matcher fires). Whether a guard is actually *enforced* is decided entirely by whether it is
present in the **per-invocation signed manifest**.

### 3.2 Per-invocation flow (in the walker, per executor invocation)

Before each `executor.invoke(inv)` (i.e. in `_execute_step`'s attempt loop, where `env = _build_env(step)`
is built, `walk.py:445-469`):

1. **Evaluate** the active set:
   ```
   active = [g for g in self.plan.guards
             if self._guard_active(g, cycle)]     # _passes_when + fail-safe (§7)
   ```
2. **Write** a signed manifest per enforced layer that this run uses, containing only `active` (filtered
   further to that layer's guards), via the existing `guards.write_manifest(...)` (HMAC over content +
   per-check-script hashes; `gatekeys.compute_content_mac`). Path = a **per-invocation** variant of
   `gatekeys.guard_manifest_path(run_dir, layer)` keyed by `step.id` + `cycle` + `attempt` so parallel
   children never share a path (§6). Stays in the gatekeys-protected location **outside** the run dir.
3. **Point** this invocation's env at it:
   `env["CAIRN_SHIM_MANIFEST"] = <path>` and `env["CAIRN_HOOK_MANIFEST"] = <path>` (per layer).
4. `executor.invoke(inv)` runs; the shim/hook subprocess reads `CAIRN_*_MANIFEST` from its inherited env
   and `_run_chain` enforces exactly the `active` set. **No subprocess change.**

### 3.3 Making the env value win (both layers must read the path from env, not a baked constant)

Today the manifest path is **baked** into both the shim template and the hook command
(`CAIRN_HOOK_MANIFEST=<static> python -m … --hook-check`, `claude.py:180`), so the static path currently
wins. Change both to **prefer the inherited env value**, with the baked path as *fallback only*:

- **Shim template** (`guards.py::_SHIM_TEMPLATE`): set `CAIRN_SHIM_MANIFEST` only if not already in env
  (`: "${CAIRN_SHIM_MANIFEST:=<baked-fallback>}"`), so the per-invocation env value from
  `GuardedExecutor` wins.
- **Hook command** (`claude.py::_hook_command`): drop the `CAIRN_HOOK_MANIFEST=<path>` prefix; let the
  hook subprocess **inherit** `CAIRN_HOOK_MANIFEST` from the claude process env (which the walker sets
  per invocation). `_hook_check` already reads `os.environ.get("CAIRN_HOOK_MANIFEST")` (with a
  `CAIRN_RUN_DIR` fallback) — no subprocess change needed.

`GuardedExecutor.invoke` (`cli.py:224-234`) currently injects a **static** `CAIRN_SHIM_MANIFEST` from
`self._delta`. Change: if `inv.env` already carries `CAIRN_SHIM_MANIFEST` (walker-set, per-invocation),
**honor it**; else fall back to the delta (a run with no runtime-`when` guards — see §8 optimization).

### 3.4 Nothing changes in the subprocess

`_run_chain`, `_shim_check`, `_hook_check`, `_load_verified_manifest`, the fail-closed logic, the
check-script hash pinning — **all unchanged**. They already: verify the manifest MAC with the per-run
secret, re-hash each check script, and run only the guards *present in the manifest*. Absent guard →
no match → allow. That is precisely the `when`-filtered behavior we want.

---

## 4. Security analysis

**Trust boundary preserved.** The *authority* over which guards are active moves to the **walker**
(trusted, in-process, holds `plan.params/dims`, calls the W2-verified gate reader). The subprocess
remains a dumb, fail-closed executor of a **signed** active-set. An agent cannot:
- Tamper the walker's in-memory `plan.params/dims` (never re-read from disk for these roots).
- Forge a per-invocation manifest (no per-run secret → MAC fails → `_run_chain` fails closed).
- Flip a gate to deactivate a guard (gates are W2-authenticated).

**Net effect on containment: neutral-to-positive.** Today a runtime-`when` guard over-blocks (safe). After
this change it activates/deactivates *correctly*, and the deactivation decision is made by the trusted
walker against authenticated inputs — never by the agent.

**Residuals (documented, not introduced by this change):**
1. **Env redirection.** An agent could set `CAIRN_SHIM_MANIFEST`/`CAIRN_HOOK_MANIFEST` for a sub-shell to
   point at an **older, still-valid signed** manifest (replay). Bounded: (a) it must be a manifest that
   already existed this run; (b) older manifests are generally *more* restrictive (fewer gates answered
   → more guards active), so replay tends to **over-block**, not under-block; (c) under
   `bypassPermissions` the agent can already edit the shim scripts / `settings.json` themselves — this is
   the existing **C8/W3c** residual, not a new hole. *Optional hardening (deferred):* bind the invocation
   id (`step`+`cycle`+`attempt`) into the signed manifest and have `_run_chain` compare it to
   `CAIRN_STEP` — but `CAIRN_STEP` is itself env, so this only raises the bar under a real sandbox; keep
   it as a noted future option, not in scope here.
2. **`run` root reads `run.json`** (agent-writable) — see §7; unchanged from today's step-`when`
   behavior, and `run`-rooted guard `when` is discouraged in docs.

---

## 5. Semantics: step-start, not fire-time

Option B evaluates `when` **when the invocation begins** (step-start), not at the moment the guarded
command actually runs (fire-time). Difference matters only if a step **produces an artifact mid-step**
that **its own guard's `when` references** — a rare, arguably ill-shaped pipeline. For the real cases:
- **Gate-based `when`** — gates are resolved *between* processes (`ARCHITECTURE §3.2`), never mid-step, so
  step-start == fire-time. ✔
- **Upstream-artifact `when`** — the referenced artifact was produced by an *earlier* step, present at
  step-start. ✔

Step-start is the correct, simple, and secure choice. Document the mid-step edge as a known, benign
limitation (the guard uses the value as of step entry).

---

## 6. Parallel isolation

`parallel:` runs child steps concurrently, each its own `executor.invoke` with its own `env`. Because
each invocation writes its **own** per-invocation manifest at a path keyed by `step.id`(+`cycle`+`attempt`)
and sets **its own** `CAIRN_*_MANIFEST` env, concurrent children never share a manifest file or env — no
race. (Contrast: a single shared per-run manifest rewritten per step *would* race under parallel; that is
exactly why the path must be per-invocation.)

---

## 7. Fail-safe on evaluation error

`_eval` currently raises `_Halt(ExitCode.CONFIG)` on an `EvalError` (used for steps — a step whose `when`
can't evaluate halts the run). For **guards**, halting the whole run because a guard's `when` couldn't be
evaluated is too aggressive, and silently *dropping* the guard would be **fail-open** (unsafe). Policy:

- **A guard whose `when` raises `EvalError` is treated as ACTIVE** (enforce it) and a `warning` is
  emitted to the trail/step log. Over-enforce on ambiguity = fail-safe, consistent with the guard
  engine's fail-closed posture.
- Implement as `_guard_active(guard, cycle)`: `when is None → True`; else evaluate via the resolver and
  return the bool; on `EvalError` → return `True` + warn (do **not** reuse `_eval`'s halting path).

Note the `run` root: `_resolver` loads `run.json` for `run.*`. run.json is agent-writable, so a
`run`-rooted guard `when` is only as trustworthy as the run dir. This equals today's step-`when` behavior;
add a doc note discouraging `run.*` in guard `when` (prefer `gates`/`params`/`dims`).

---

## 8. Edge cases & optimizations

- **No runtime-`when` guards (the common case).** If every guard is static (`when is None`), the active
  set is identical every invocation → write the manifest **once** (as today) and let `GuardedExecutor`'s
  static delta / the baked hook path stand; skip per-invocation writes entirely. Gate the per-invocation
  path on `any(g.when is not None for g in plan.guards)`. Zero overhead + zero behavior change for
  pipelines without runtime-`when` guards (i.e. essentially all existing ones).
- **Loops.** `cycle` is bound in the resolver; a guard `when: cycle >= 2` or referencing
  `artifacts.x-r{cycle}` evaluates per cycle correctly (the walker passes `cycle`).
- **Manifest cleanup.** Per-invocation manifests accumulate in the gatekeys dir. Sweep them with the
  run (extend the existing gatekeys/gc cleanup to remove `<run_id>-*` manifest files on run
  completion/gc). Must not break resume (resume re-writes per invocation anyway).
- **Both layers.** A guard may be `shim`- and/or `hook`-enforced; write/point the per-invocation manifest
  for each layer the run uses, reusing the single `write_manifest` builder for both (as W3a already does).

---

## 9. Implementation plan (files & sequence)

Sequenced so each step is independently testable; total is a focused refactor, not a rewrite.

1. **`plan.py` / `guards.py` — carry `when` source to the walker, not the manifest.**
   - No change to how `GuardDecl.when` reaches `self.plan.guards` (already a live `Expr`). Confirm the
     manifest still never serializes `when` (it must not — `when=None` on reload stays correct).
   - Add a NOTE at `_load_manifest_guard` that `when` is intentionally absent (walker pre-filters).
2. **`walk.py` — evaluate + write per-invocation manifest.**
   - Add `_guard_active(guard, cycle)` (fail-safe active on EvalError + warn) reusing `_resolver`.
   - Add `_active_guard_manifest(cycle, step, attempt)`: compute active set; if `any(g.when …)`, write
     signed per-invocation manifest(s) via `write_manifest` at per-invocation `guard_manifest_path`, and
     return the `{CAIRN_SHIM_MANIFEST, CAIRN_HOOK_MANIFEST}` overrides; else return `{}`.
   - Merge those overrides into `env` before building `Invocation` (`walk.py:445-469`).
3. **`gatekeys.py` — per-invocation manifest path.**
   - Extend `guard_manifest_path(run_dir, layer, *, key=None)` (or a sibling) to produce a per-invocation
     path when `key` is given; keep the no-key path for the static/once case.
4. **`cli.py` `GuardedExecutor.invoke` — honor a walker-set shim manifest.**
   - If `inv.env` has `CAIRN_SHIM_MANIFEST`, use it; else inject the static delta value (today's behavior).
5. **`guards.py` `_SHIM_TEMPLATE` — env-first manifest path.**
   - `: "${CAIRN_SHIM_MANIFEST:=<baked-fallback>}"` so a per-invocation env value wins; baked = fallback.
6. **`claude.py` — hook command inherits `CAIRN_HOOK_MANIFEST`.**
   - Drop the `CAIRN_HOOK_MANIFEST=<path>` baked prefix from `_hook_command`; rely on the inherited
     per-invocation env (walker-set). Keep the once-per-run `settings.json` install + the static hook
     manifest as the fallback for the no-runtime-`when` case.
7. **Cleanup + docs** (§8, §11).

---

## 10. Test plan (Done-when)

- **Runtime gate `when`, active → inactive:** guard `when: gates.g.choice=='yes'`; before the gate is
  answered the guard's check RUNS on a matching command (denies); after `answer_gate(g=yes)` a later
  invocation's manifest OMITS the guard → the command is ALLOWED. (Walker-level test driving two
  invocations across a gate answer.)
- **Runtime artifact `when`:** guard `when: artifacts.qa.verdict=='NO-GO'`; active only when the artifact
  says NO-GO; flips with the artifact content.
- **Static guard unaffected:** a `when`-less guard is present in every invocation's manifest and enforced
  exactly as today; the no-runtime-`when` fast path writes the manifest once (assert no per-invocation
  churn).
- **Parallel isolation:** two parallel children with different active sets get distinct manifests; each
  enforces its own set; no cross-contamination / race.
- **Fail-safe:** a guard whose `when` raises `EvalError` (e.g. references a missing artifact) is treated
  ACTIVE (enforced) + a warning is emitted — NOT dropped, NOT a run halt.
- **Tamper still fails closed:** the per-invocation manifest is MAC-signed; a tampered/forged one → the
  shim (exit 2) and hook (deny-JSON) fail closed exactly as W3a (re-run the W3a tamper tests against a
  per-invocation manifest).
- **Security review reproduction:** confirm an agent cannot deactivate a guard by editing `run.json`
  params (the walker uses in-memory `plan.params`, not the file) nor by forging a manifest.
- **Full suite green** (`uv run pytest tests/unit -q`, 0 failed) + no regression in the W2/W3a gate &
  guard auth tests.

---

## 11. Docs to update on landing

- `ARCHITECTURE.md §4` (guard matrix) + a short "guard activation" note: runtime `when` on a guard is
  evaluated by the walker per invocation against trusted state; the per-invocation signed manifest carries
  only active guards.
- `SECURITY.md §2`: the active-set authority is the walker; the subprocess remains a signed, fail-closed
  executor; note the env-redirection residual (§4.1) and the `run.*`-in-guard-`when` caution (§7).
- `IMPLEMENTATION-PLAN.md`: move **C9** from *deferred* to *done* (or *in progress*) with a pointer here.
- Remove the "runtime-`when` guard over-applies" limitation wording wherever it was recorded.

---

## 12. Risks & rollback

- **Touches the W3a-hardened manifest path.** Mitigation: the subprocess (`_run_chain`) and the
  MAC/hash verification are **unchanged**; only *where and how often* the (identically-signed) manifest is
  written moves. The blast radius is the manifest-write timing + the env-path plumbing, both behind the
  existing signature check.
- **Env-first path plumbing could regress the static case.** Mitigation: gate per-invocation writes on
  `any(g.when …)`; with no runtime-`when` guards the code path is byte-identical to today (baked/static).
- **Rollback:** the change is additive behind the `any(g.when …)` gate; reverting the walker's
  per-invocation write + the env-first shim/hook lines restores exact W3a behavior.
- **Review:** land via a fresh implementer + a dedicated **security review** (tamper-safety of the
  per-invocation manifest, no fail-open introduced, parallel isolation, fail-safe-on-EvalError), mirroring
  how W2/W3a were reviewed.

---

## 13. Recommendation

Proceed with **Option B**. It corrects the runtime-`when` semantics, *reduces* rather than grows the
attack surface in the hardened path (subprocess unchanged; authority stays in the trusted walker), reuses
the existing tested `_passes_when` machinery, and is safely gated so pipelines without runtime-`when`
guards are entirely unaffected.

**Landed 2026-07-15, as designed above (Option B).** `_guard_active`/`_active_guard_manifest`
(`walk.py`), the `key=` per-invocation `guard_manifest_path` (`gatekeys.py`), the env-first shim
default (`guards.py::_SHIM_TEMPLATE`) and `GuardedExecutor.invoke` (`cli.py`), and the
env-inherited hook command (`claude.py::_hook_command`) all match §3–§9 as specified; the seven
§10 test-plan items are covered in `tests/unit/test_walk.py` (§15) and `tests/unit/test_guards.py`
(the per-invocation-manifest tamper + `guard_manifest_path` key-sanitization tests). See
`IMPLEMENTATION-PLAN.md`'s C9 entry for the landing summary.
