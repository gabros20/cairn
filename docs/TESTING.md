# cairn — Testing & Validation

The full validation story, cheap to expensive. Two distinct testing concerns, kept separate:

- **The framework's own tests** — the C1 synthetic suite (IMPLEMENTATION-PLAN): fixture pipelines
  exercising every kernel semantic via the `shell` executor. Lives in the cairn repo; gates every
  release. Not the subject of this doc.
- **A workspace's tests** — how a pipeline author knows *their* pipelines, validators, guards, and
  envelopes are correct **before spending a token**. That's this doc: the `tests/` convention +
  `cairn test` + the `stub` executor.

## 1. Why a test layer at all — the false-green problem

Validators are the sole arbiter of done-ness (ARCHITECTURE §3.1): every resume decision, every
hard gate, every loop exit trusts them. That gives validators the highest blast radius of any code
in a workspace, in both failure directions:

- **False-green** (validator passes a broken artifact) — the worst failure mode an autonomous
  pipeline has: the bad artifact *propagates*, poisoning every downstream phase, and surfaces as a
  confusing failure hours later (a P2 catalog validator that misses a bad section key wrecks P4).
- **False-red** (validator rejects a good artifact) — halts autonomous runs, erodes trust in
  halts, invites `--force` culture.

Untested validators mean the entire enforcement doctrine rests on unreviewed code. The same logic
applies to guards (an F18 check that silently allows is worse than no check — it *claims* safety).
So: the artifacts get validators, and **the validators get fixtures**.

## 2. The validation pyramid — five layers, priced

| | Layer | Verifies | Cost | Cadence |
|---|---|---|---|---|
| L0 | `cairn plan` | config: dataflow, references, expressions, schemas exist | 0 tokens, ms | every edit |
| L1 | `cairn test` | validators, guards, envelopes, **pipeline wiring via stub runs** | 0 tokens, seconds | every commit (CI) |
| L2 | `cairn doctor` | the machine: executor auth/versions, hook probe, `[tools]` | 0 tokens | per machine / setup change |
| L3 | live slice (`cairn run --from X --to Y`) | one new/changed step against a real model | few steps' tokens | per feature |
| L4 | full live run (+ golden-run diff, §6) | the whole pipeline incl. model behavior | full run | per milestone / release |

The layers are strictly ordered: nothing at L(n) should be discoverable at L(n−1). L0–L2 already
existed in the design; L1's `cairn test` is specified below; L3/L4 are practices, not machinery.

## 3. The `tests/` convention

```
tests/
├── fixtures/<artifact>/valid-*.{json,md} invalid-*.{json,md}   # validator + schema fixtures
├── guards/<guard>/allow-*.json deny-*.json                     # guard-check fixtures (stdin payloads)
├── stubs/<pipeline>/<step>[.c<cycle>]/…                        # canned artifacts for stub runs
├── envelopes/<step>.golden.md                                  # composed-envelope snapshots
└── matrix.yaml                                                 # param sets for stub runs
```

```yaml
# tests/matrix.yaml — which param combinations the stub runs exercise
brease-rebuild:
  - { mode: rebuild,   brease: "off" }
  - { mode: redesign,  brease: "off" }             # exercises escalation + the art-review loop
  - { mode: reimagine, brease: "off" }             # exercises strategy + full conditional chain
  - { mode: redesign,  brease: "on", _gates: { populate-approval: "no" } }   # quote YAML-boolean enums (API.md §2.1)
```

## 4. `cairn test` — four suites, one command

```
cairn test [validators|guards|pipelines|envelopes] [--pipeline P] [--update]
```

**4.1 validators** — for every artifact with fixtures: each `valid-*` must pass; each `invalid-*`
must fail **with at least one reason line** (a rejection without a reason is itself a failure —
reasons feed retry envelopes and humans). Kills false-green and false-red in one suite.

**4.2 guards** — each guard check run against its `allow-*`/`deny-*` stdin payloads; deny cases
must produce a stderr reason. Verifies the check *logic*; whether the hook layer fires on a given
CLI remains L2's probe (doctor) — logic and wiring are different questions, tested at different
layers.

**4.3 pipelines (stub runs)** — the centerpiece. Each matrix row runs the pipeline with the
**`stub` executor** in a throwaway run dir: every agent step, instead of invoking a model, copies
`tests/stubs/<pipeline>/<step>[.c<cycle>]/` into the run dir and returns a canned STEP. Everything
else — planner, walker, gates (resolved from `_gates`/defaults), loops, conditionals, `run:` steps,
schema+validator evaluation, trail, resume — is **production code**. One offline suite therefore
verifies: step order and runtime dataflow, conditional chains per mode, loop mechanics (per-cycle
stub dirs let `until:` fire on cycle 2, or never, to test `on_cap`), gate plumbing, and that the
stub artifacts themselves satisfy the schemas+validators (fixtures and contracts can't drift
apart silently).

**4.4 envelopes** — `cairn compose <pipeline> <step> --params …` renders the six-block envelope
without executing; diff against `tests/envelopes/<step>.golden.md` (`--update` to accept). Catches
the quiet killers: a skill edit bloating every prompt, a doctrine change dropping the tripwire, a
contract description going stale. Snapshots make prompt changes *reviewable in diffs*.

## 5. The `stub` executor + `cairn test record`

`stub` is a built-in executor implementing the normal protocol (`capabilities: {blocking_hooks:
false, output_schema: false, session_capture: None}`), selectable like any other — which also
means a *human* can `cairn run … --executor stub` to watch a full pipeline "execute" offline.

Stubs come from reality, not imagination: **`cairn test record <run-dir>`** harvests a completed
real run into `tests/stubs/` + `tests/fixtures/` (`--slim` truncates bulky payloads — image
binaries become 1-px placeholders; validators must not depend on payload weight). The workflow:
first real run of a new pipeline → record it → wiring is regression-locked forever at zero tokens.
Invalid fixtures are then authored by deliberately breaking copies of recorded ones ("what must
this validator catch?") — each `invalid-*` fixture documents a failure class.

## 6. L3/L4 practice (no machinery)

- **L3 — live slice:** after changing a step, `cairn run --from <step> --to <step>` against the
  designated smoke target (a small, stable, authorized site pinned in the workspace README). One
  step's tokens, real model, real tools.
- **L4 — golden-run diff:** keep one recorded reference run per pipeline per release. After a full
  live run, `cairn validate` everything, then diff *structure* against the golden run (artifact
  set, section-key sets, route lists — not prose). Drift is reviewed, not auto-judged: **no
  LLM-judge evals in v1** — the pipeline already contains its own judges where judgment is needed
  (P4.5 art review, P5 QA), and duplicating them in the test layer would drift.

## 7. CI shape

```yaml
# workspace CI — no tokens, no secrets, < 1 min
- uv tool install cairn…                # pinned by requires
- cairn plan --all                      # L0 over every pipeline
- cairn test                            # L1: validators · guards · stub matrix · envelopes
```
The cairn repo's own CI additionally runs the C1 synthetic suite. L2–L4 are human-triggered by
design: they need machines, auth, and budget — CI proves *correctness of the workspace*, not *the
health of a machine* or *the behavior of a vendor model*.

## 8. Non-features, named

No mocking framework (the stub executor **is** the mock, via the production protocol); no
LLM-judge test harness (§6); no coverage metrics over prompts; no parallel test DSL — `tests/` is
fixtures + one matrix file, and `cairn test` has no options beyond suite selection and `--update`.
