# FACTORY-PLAN — the factory layer: queue semantics, the judgment inbox, autonomy lanes

Status: DRAFT v5 (four panel rounds; stability-hardened; ready for implementation).
Provenance: three-model panel (grok-4.5, GPT-5.6-sol, Claude Fable 5) evaluating cairn
against Addy Osmani's "Software Factories, Light and Dark" (July 2026); all rounds in
`.orchestrate/factory-panel/`. v1→v2 reviews (QTP, taxonomy, admission). v2→v3
adversarial pass (durability, linkage, identity, gc, multi-factory). v3→v4 full-system
sweep under the refactor mandate (W0 = kernel refactors). v4→v5 stability sweep — the
"hostile week" round: bounded state everywhere, no absorbing states, adversarial
admission, upgrade safety. Round-4 verdicts: unanimous APPROVE-WITH-CHANGES (~92%
as-written; the additions below are the named path to 100% operational stability
within the single-machine doctrine). All code claims controller-verified.

## 0. Doctrine (binding constraints — every wave must hold all of these)

- D1 **Local-first, no resident process.** The host (launchd/systemd) wakes cairn;
  cairn exits. No daemon, no socket the walker listens on, no broker in the kernel.
- D2 **The filesystem is the one authority — on a real local filesystem.** Queue
  state = files + atomic rename/hard-link. No SQLite, no Redis. Watch/ledger dirs
  under cloud-sync roots or failing the hard-link probe are a HARD refusal at
  `run`/`sync` (logged `--unsafe-synced-fs` override exists). Ledger dirs `0700`,
  records `0600`, cairn-owned: hand-editing is documented as unsupported; the
  integrity audit (T8) quarantines rather than trusts. Derived indexes only ever
  as caches.
- D3 **Four node shapes, frozen.** No `queue:` node. The queue lives at the
  triggers layer; work items enter pipelines as param → validated artifact.
- D4 **Validators own "done".** Retirement keys off child exit codes (exit 0 =
  all produces validated). Delivery receipts are ordinary `produces:`. External
  effects are at-least-once made safe by resume-not-redeliver (T4) + per-effect
  idempotency markers that FAIL CLOSED (W4): only an authoritative "absent"
  answer may create; lookup uncertainty parks BLOCKED. The honest guarantee is
  "current at revision-check time", recorded in the receipt.
- D5 **Brokers and cloud apps are bridges, not deps.** Adapters are workspace
  furniture. A broker consumer may drop files into an inbox; cairn never speaks
  AMQP/Redis.
- D6 **Back pressure is the product — and it is BOUNDED end to end.** (v5)
  Every state a work item can occupy has a cap, an age surface, or both: nothing
  in the factory grows without bound and nothing waits without a visible age.
  Generation stops when judgment is full; pullers stop when the spool is full;
  admission stops when the machine is full. Freed capacity wakes via the
  reconcile beat (host-woken, D1).
- D7 **Behavior-compatible, tests-honest.** New behavior opt-in; defaults
  preserve observable semantics. Tests may be rewritten with the code they test;
  coverage never shrinks, assertions never weaken; goldens update with stated
  rationale.
- D8 **One exit-code taxonomy end-to-end, as code.** `RunOutcome` (W0):
  `done | waiting(needs_human | capacity | blocked) | failed`. Waiting-class =
  `NEEDS_HUMAN(6)`, `CAPACITY(8)`, `BLOCKED(9)`. The walker emits it, the ledger
  routes by it, gc pins by it, the inbox renders by it. The outcome class is
  also WRITTEN INTO the ledger pointer record at retire (v5) so depth counts
  never re-read trails. Transient trouble parks; it never poisons.
- D9 **Name the boundaries honestly.** Factory = one workspace. The target repo
  is a shared machine-wide resource (W8 leases + worktrees). The machine is the
  capacity boundary (workspace-UUID host units, machine pool). One upstream
  source feeds ONE factory (W4 doctrine). N factories on one machine is a
  supported, tested mode.
- D10 **Refactor the source; never detour.** Awkward primitives are refactored
  in place: no monkey-patching, no wrapper piles, no two APIs for one seam.
  Tests inject seams (fs/journal fake), never monkeypatch `os.*`.

## 1. Vocabulary

- **Work item** — one JSON file in a trigger's watched inbox
  (`schemas/work-item.json`). Canonical identity `(source, source-id)` (T1).
- **Rev** — the item's upstream version, SORTABLE: `r<epoch>` derived from the
  provider's `updated_at` (never a hash — two mechanisms compare revs for
  order/coverage; v5). Ties extend with the provider's own version counter
  where one exists.
- **Lane** — a named autonomy profile; `lit` ⇄ `dark`.
- **Waiting** — parked resumably: needs-human (6), capacity (8), blocked (9).
- **Judgment inbox** — `cairn inbox`: the human drain. The amber box, tooled.
- **Factory** — one workspace. **Repo lease** — canonical-path lock (W8).
- **Spool** — everything pre-claim: inbox + `.deferred/`. Bounded (T2).

## 2. The Queue Transition Protocol (QTP)

Every transition named, atomic, durable, crash-recoverable; tested via injected
fs/journal fakes with a crash between every op pair and replay-loss (D10).

**Lifecycle (pinned in full — v5):**
`inbox → reserved+claimed → running ⇄ waiting(6|8|9) → done | failed`;
newer live revision → `.deferred/` → promoted to inbox on reservation release;
nonconforming input → `.rejected/` (bounded, never reserved);
aged-out failed → tombstoned archive (retention, T3).
Every state is reachable AND leavable; the only sinks are `done`, `failed`
(aged + alarmed), and `rejected` (bounded). The three absorbing traps found in
round 4 — orphan reservations, unreleased gc pins, unaged `.failed/` — are
closed by T1/T3/T8 below.

- **T0 — Durability primitive.** All moves via `durable_move()` in the shared
  durable-fs module (W0.3): link/unlink/rename + fsync of affected dirs in
  dependency order (modeled on runstate.py:54; triggerkit today has none).
  Replay-loss harness in T7.

- **T1 — Identity, revision, priority, admission envelope.**
  - Identity = `<source>-<id>` (lowercased, case-safe). Filename =
    `p<prio>-<source>-<id>-r<rev>.json`; single-digit prio; STRICT grammar with
    a total length budget (identity + every ledger suffix — `.ids`, pointers,
    tombstones — must fit NAME_MAX; ENAMETOOLONG mid-QTP is a hazard, v5).
  - **Admission envelope (v5):** at scan, before any reservation: regular file
    (no symlink), byte cap, UTF-8 JSON, filename grammar parse, and
    filename↔body agreement on (source, id, rev, prio). Nonconforming →
    `.rejected/` (bounded dir, surfaced by `trigger list`), never claimed,
    never reserved. Traversal names are structurally dead (names come from
    readdir) — stated, not assumed. Identity spoofing across sources is a
    TRUST-BOUNDARY note: pullers are trusted code; optional per-trigger
    `sources: [github]` allowlist narrows it.
  - **Reservation is durable:** `.claim/.ids/<identity>` by O_EXCL before
    claim; released only per T3. **Orphan recovery (v5):** the sweep releases a
    reservation with no live item (claim/waiting) anywhere after a grace
    period — reservation-orphans must not be absorbing.
  - **Re-entry:** a new rev of a retired identity admits; a rev ≤ any tombstoned
    rev for that identity is skipped and removed.
  - **Deferred revisions:** a new rev arriving while the identity is live goes
    to `.deferred/<identity>` (one file, latest SORTABLE rev wins — well-defined
    now that revs order, v5). **Promotion is one ledger transaction keyed to
    reservation release** (both done AND failed retirements): select newest
    deferred → compare against the retiring run's receipt-checked rev —
    **≤ checked rev ⇒ tombstone it** (already delivered; prevents the
    duplicate-delivery chain: park → pre-delivery refresh delivers newest →
    deferred same-newest would otherwise re-run) — else install into the inbox
    while transferring reservation ownership, then release.
  - **Priority change:** puller renames (atomic; lost race = claimed, benign —
    verified triggerkit.py:320-340). **Aging (v5):** `order: aged` (factory
    scaffold default) computes effective priority in-memory from prio and file
    mtime at scan — steady p1 arrivals cannot starve p9 forever. `order: name`
    remains, with starvation documented.
  - **Tombstones on EVERY terminal retire** — done, failed, cancelled (v5 pins
    the v4 T1/T3 contradiction) — even under `on_done: delete`.

- **T2 — Admit, bounded (v5).** Serial admitter: reserve → claim → submit, one
  at a time, each step re-checking caps NOW:
  - `waiting_max` — needs-human depth (judgment lane);
  - `blocked_max` (default = waiting_max) — blocked depth, own diagnostic;
  - `capacity_max` — CAPACITY parks now count (v4 exempted them; round 4 showed
    the amplification: zero slots + full inbox = thousands of pinned run dirs);
  - `wip_max` — total live items (claimed + all waiting classes);
  - `inbox_max` — spool cap consulted by pullers (W4);
  - capacity-aware minting: don't mint a run when the slot pool reports zero
    free agent slots and the pipeline's first actor is an agent step — parking
    at birth is churn, not progress.
  Depth counting reads pointer-record outcome classes (D8), one readdir per
  ledger dir. Concurrent drains overshoot ≤1 each — documented, accepted.

- **T3 — Retire.** Exit 0 → `.done/`; 6/8/9 → `.waiting/`; other nonzero →
  `.failed/` (pointer moves too). Pointer record content = outcome class + exit
  code (D8). Tombstone always (T1). **Pin-release ordering (v5):** terminal
  ledger placement FIRST, then the run's reciprocal gc pin is durably cleared
  (`queue-released` in run meta — the drain owns both sides); reconcile repairs
  a crash between the two (T8 audit finds terminal-ledger + still-pinned runs).
  Reservation released on terminal retire (with deferred promotion, T1);
  retained across `.waiting/`. **Failed-retry identity rule (v5):**
  `trigger retry` reacquires the identity reservation; if a newer rev now holds
  it, the retry is refused into `.deferred/`-style parking — one identity never
  runs twice concurrently. **Failed retention (v5):** `.failed/` is alarmed by
  age and pinned-bytes in `trigger list`/reconcile output; optional
  `failed_archive_days: N` tombstones + releases the pin (documented as
  evidence expiry). `.rejected/` and tombstones get gc presets (W1d).

- **T4 — Resume, never redeliver.** Unchanged: any item whose child minted a
  validated `run.json` is continued via resume; a dir without validated
  run.json is a husk — reclaimed, never "resumed". Redelivery only when no run
  exists.

- **T5 — Run linkage.** Drain computes `<trigger>-<identity>-r<rev>`, durably
  writes `.claim/.runs/<name>` BEFORE spawn, spawns via ProcessHandle with the
  explicit run dir (`--run-dir` exists — cli.py:502; `RunController.mint_new`
  makes minting atomic), child pid lands in the lease immediately. Pointer
  moves pointer-first on retire. Pointer repair, not deletion. **Resume
  concurrency (v5):** sweep, reconcile, and inbox may target one run — resume
  goes through a nonblocking run-lock; contention means "still active", never
  BLOCKED or failed.

- **T6 — Sweep.** Route by recorded `run-halt` exit code (already recorded —
  walk.py:528; never `derive_status`). Guard-refusals (drift/version/tools) are
  NOT outcomes: item stays parked, refusal recorded on the item, card shown; no
  retry storm. **Blocked probes (v5):** every BLOCKED item carries
  `next_probe_at` (mandatory) with exponential backoff CAPPED at 4h; the beat
  probes only when due, one probe per (probe-key, beat), cached. **Capacity
  resume budget (v5):** at most `free_slots` capacity items resumed per beat,
  selection jittered per workspace — no thundering herd of spawn→wait→park
  churn across factories.

- **T6b — Staleness contract.** Refresh ALWAYS produces `source-status`
  (`current | changed | closed`) — never a bare `skippable` (walker `needs`
  would strand). `closed` → cancellation path → `cancelled` tombstone.
  `changed` → the card demands "accept new revision" = `RunController`
  invalidate-to-intake ON THE SAME RUN (never a second run; T4 holds — v5 pins
  the mechanics). **Fingerprint exclusion (v5):** the actionable revision
  fingerprint EXCLUDES cairn-authored effects (its own comments/labels/PR
  links) — else every write-back requeues the issue forever. Receipts record
  marker, provider object id, checked rev.

- **T7 — Crash + durability tests.** Injected fs/journal fake; named cases:
  pointer/item pair, ENOSPC, reboot mid-child, gc-vs-pointer, `on_done:
  delete` + re-entry, deferred promotion vs concurrent puller drop, reservation
  crash windows (orphan recovery both directions: reservation-without-item,
  item-without-reservation), pin-release crash, ENAMETOOLONG hazard.

- **T8 — Ledger integrity & versioning (new in v5).**
  - **`ledger-version` marker** in each watch dir; a drain REFUSES a ledger
    newer than it understands; `trigger sync` stamps/bumps it. Closes the
    mixed-version upgrade hole (an old binary would retire exit-6 to `.failed/`
    and corrupt a v5 ledger).
  - **Invariant audit** (in `cairn doctor` + the reconcile beat, single-flight
    per workspace): every pointer has an item and vice versa; every claim has a
    reservation; no identity in two states; terminal-ledger runs are unpinned;
    outcome classes parse. Violations → quarantine + surfaced, drain refuses
    only the affected identities. Reconcile's own host unit is removed when the
    last queue-keyed trigger is removed.

## 3. W0 — Kernel refactors [P0, first]

1. **`ProcessHandle` runner** — `Runner.spawn() -> ProcessHandle` (pid, poll/
   wait, streams); `run()` reimplemented over it; all call sites + fakes move
   (proc.py:62 today). No side-by-side APIs.
2. **`RunController`** — cli.py's run/resume entrance (cli.py:500-566/:791-833)
   extracted to kernel: `mint_new` (atomic), `resume_existing` (guards, typed
   refusals), invalidate-to-intake (T6b). CLI, drain, inbox all call it as a
   library.
3. **Durable-fs module** — `durable_move()` + atomic writes unified with
   runstate `_atomic_write`; consumed by runstate, gatekit, ledger, leases,
   slots.
4. **`RunOutcome` taxonomy** — `classify(exit_code)` in types.py + new
   `CAPACITY(8)`/`BLOCKED(9)`; `retire(outcome)` REPLACES `consume(ok=)`;
   `derive_status` rebased for parked classes, retained for presentation only.
5. **triggerkit split** — `queue_ledger.py` (QTP), `queue_drain.py` (admission/
   pool/leases), `trigger_host.py` (parsing + host units) — before W1 grows it.

## 4. W1 — Truthful states [P0]

W1a ledger routing per T3/T5/T6 (today exit 6 → `.failed/`, triggerkit.py:1046).
W1b defaultless gates (plan.py:786 blocks gatekit's built park path,
gatekit.py:80-127). W1c BLOCKED(9) (auth classifier exists — walk.py:690).
W1d gc pinning, reciprocal + RELEASABLE: RunOutcome-classified protection
(gckit.py:42 protects only `gate` today); pins written at mint, cleared per T3,
audited per T8; gc presets ship retention recipes for tombstones, `.rejected/`,
`failed_archive_days`. `cairn trigger list` shows all depths + ages + alarms
(pending/claimed/waiting/blocked/capacity/deferred/failed/rejected, stuck
claims, lease ages, pinned bytes, recorded refusals).

## 5. W2 — `cairn inbox` [P0]

As v4 (cards, inline answers, `--resume` + post-resume origin sweep,
`--list`/`--json`, drift card, `--failed` retry surface, STALE/changed per T6b,
RunController as a library). v5: the inbox header surfaces depth/age ALARMS
(operator-absence pathologies must be visible at the one surface the human
opens — waiting age, failed pile, spool depth). E2e acceptance incl. the LIVE
vendor smoke: park → inbox → answer → resume → delivery.

## 6. W3 — Queue semantics [P0]

v4 keys + v5 caps (`capacity_max`, `wip_max`, `inbox_max`, `order: aged`).
Leases: child pid + boot time + start token; default ON for ALL queue-keyed
triggers (round-4 grok — not just concurrency/lane); `lease: off` restores
today. Host units: persistent workspace UUID in UNCOMMITTED local state
(duplicate-UUID detection via the machine registry — copied workspaces get
re-minted on first sync). Reconcile beat rendered by schedkit under the ws
label, single-flight, runs the T8 audit. Shared watch dirs → ConfigError.
Doctor: D2 hard refusals + T8 audit. Global `[factory] waiting_max` across the
workspace's triggers.

## 7. W4 — Work-item contract + source pullers [P1]

As v4 (schema, scaffolds, env-only tokens, cursor completeness with
`(updated_at, id)` + overlap window, T6b hooks, deterministic PR markers,
separately receipted delivery/notify, one-source-one-factory doctrine). v5:
- **Puller backpressure contract:** before polling, the scaffold checks spool
  depth (`inbox_max`); over cap → skip the poll entirely (cursor untouched —
  safe by design, nothing lost upstream) and exit 0 with a `paused` poll
  report. Pullers never advance a cursor past unemitted items.
- **Markers fail closed** (D4): find-before-create where only authoritative
  absence creates; uncertainty → BLOCKED. Every external effect (PR, comment,
  label) has its own marker keyed identity+rev.
- **Fingerprint excludes cairn-authored effects** (T6b).
- Rev derivation: `r<epoch>` from provider `updated_at` (+ provider version
  counter tiebreak). Puller-side aging optional on top of `order: aged`.

## 8. W5 — Autonomy lanes [P1]

As v4 (plan-time preset resolution, `by: "lane:<name>"` additive, validator-
bearing preset reads, `gates: {}` error, `max.headless` caps). v5: optional
**dark-lane circuit breaker** — `lane_circuit: {failures: N}` pauses admission
for that trigger after N consecutive failed dark runs (diagnostic, resumed by
operator or a passing lit run). A dark lane that fails all night must not burn
the queue.

## 9. W6 — Capacity: agent slots, machine pool [P1]

As v4 (numbered O_EXCL slots, heartbeat, wait excluded from timeout, expiry →
CAPACITY(8), machine config authority `~/.cairn/machine.toml`, join-by-
presence, per-executor sub-pools). v5 pins: the pool dir is created by
`trigger sync` when factory keys are present (bootstrap ownership); slot-pool
opt-out NEVER opts out of W8 repo locks; the drain's capacity-aware admission
(T2) and the beat's resume budget (T6) read free-slot counts from here.

## 10. W7 — `cairn stats` [P2]

As v4, plus per-state depth/age time series (the alarms' history) and
blocked-time, lease-reap, circuit-breaker counts.

## 11. W8 — Repo leases + worktree doctrine [P1]

As v4 (canonical git-common-dir lock resolution; opaque locks for non-repo
resources; plan-time ERROR for concurrent/dark git-touching pipelines without
locks/worktrees; worktree lifecycle in run meta + gc). v5 pins: "touches a git
dir" = the workspace root or any step `run:`/cwd resolving under a git
common-dir (no false ERROR on docs-only pipelines); multiple `locks:` acquire
in canonical sort order (no deadlock); all held locks RELEASE before any park
(a parked run holds judgment, never a repo); hung-holder ttl surfaced in
`trigger list`.

## 12. Explicitly out of scope

No daemon; no broker/SQLite authority; no fifth shape; no multi-machine claims;
no cross-process admission lock; no kernel git wrapper; content-hash `key:`,
named API rate leases: P2.

## 13. Sequencing & gates

**W0 → W1 → W2 → W3** strictly ordered; W2 carries the live vendor smoke.
W4/W5/W8 parallel after W3 (W8 lease primitive may land with W6); W6/W7 after
W3. Every wave: T7/T8 tests where touched, full suite green (D7), docs retold
in the same commit.

## 14. Panel record

**Round 1 (v1→v2), A-W-C ×3:** exit-taxonomy routing (Fable) · crash-consistent
QTP (GPT) · lazy admission counting inflight (grok) · dot-dir pointers (all) ·
resume-not-redeliver (GPT/Fable) · ledger dedupe (GPT) · preset-reads oracle
lint (Fable/grok) · CAPACITY + slots (Fable/GPT) · W1→inbox→queue (Fable) ·
honesty rename (GPT). Rejected: admission flock; queues.yaml layer.

**Round 2 (v2→v3):** T0 durability (GPT) · handle-based linkage — v2 T5
unimplementable on blocking run (GPT) · identity/rev/tombstones (all) · pointer
repair (Fable) · gc pinning (all) · BLOCKED(9) (all) · staleness T6b
(GPT/Fable) · cursor completeness (grok/GPT) · scoped labels (all) · reconcile
beat (GPT) · leases default-on (grok) · sync-root refusal (grok/Fable) ·
machine pool (all) · W8 (all).

**Round 3 (v3→v4), 88/88/86 A-W-C ×3 — the refactor round:** W0 kernel
refactors: ProcessHandle, RunController, durable-fs, RunOutcome replacing
`consume(ok=)`, triggerkit split (all) · D7 amended; D10; injected fakes (all) ·
`--run-dir` existed — respec + drain-side naming (Fable/grok/GPT) · O_EXCL
reservations (GPT) · `.deferred/` (GPT) · source-status respec (GPT) ·
guard-refusals park (Fable) · recovery probes + cached doctor (GPT/Fable/grok) ·
waiting/blocked split (grok/Fable) · schedkit-rendered beat, shared-watch
ConfigError (Fable) · ws UUID, reciprocal pins, common-dir locks, notify
markers (GPT) · W8 ERROR (grok) · join-by-presence (Fable/GPT) · one-source-
one-factory (Fable) · smoke test (Fable) · run-halt exit_code +
bootstrap_run(run_dir=) verified present (grok).

**Round 4 (v4→v5), A-W-C ×3 — the stability round (~92% as-written):**
bounded state everywhere: `capacity_max`/`wip_max`/`inbox_max`, capacity-aware
minting (grok/GPT/Fable) · priority aging `order: aged` (all) · retention:
failed aging + alarms, tombstone/rejected gc presets, pinned-bytes surfacing
(all) · admission envelope + `.rejected/` quarantine + NAME_MAX budget
(GPT/Fable/grok) · sortable rev `r<epoch>` — hash revs cannot order; receipt-
aware deferred promotion kills the duplicate-delivery chain (Fable/GPT) ·
promotion as one transaction keyed to reservation release, both terminals
(GPT/Fable) · reservation-orphan recovery (Fable/GPT) · pin-release protocol +
ordering (Fable/GPT) · tombstone every terminal — v4 T1/T3 contradiction
(grok/Fable) · `next_probe_at` mandatory, backoff cap 4h (all) · capacity
resume budget + jitter (Fable) · fail-closed markers (GPT) · fingerprint
excludes cairn-authored effects (GPT) · failed-retry reacquires identity (GPT) ·
nonblocking run-lock on resume paths (GPT) · T8 ledger-version marker — mixed-
version upgrade corruption (Fable) · T8 invariant audit + quarantine, single-
flight beat, unit cleanup (GPT/grok) · pointer records carry outcome class
(Fable) · trust-boundary + `sources:` allowlist (grok) · puller backpressure
contract, cursor never bypasses unemitted items (grok/Fable) · dark-lane
circuit breaker (GPT) · lock sort order, release-before-park, git-dir
definition, pool bootstrap ownership, opt-out never exempts repo locks
(GPT/grok) · inbox depth/age alarms (grok/Fable). Residual, accepted: human
judgment latency is the non-scalable box by design (Osmani); multi-machine and
content-hash keys remain out of scope.
