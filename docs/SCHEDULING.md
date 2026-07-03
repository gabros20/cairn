# cairn — Scheduling

First-class scheduled runs **without a scheduler**. cairn does not own a clock, run a daemon, or
manage a queue — the host already has a battle-tested scheduler (cron / launchd / systemd timers).
cairn's job is to make itself *perfectly schedulable*: declared schedules in the workspace,
one-command installation into the host, and idempotent invocations that a timer can fire blindly.
Lineage: `certbot renew`, `restic` + systemd timers, terraform-in-CI — the tool is
schedule-*ready*; the platform schedules.

> **Status: designed; lands C6 — see IMPLEMENTATION-PLAN.** The `cairn schedule` verb is a stub
> today (exit 2); `schedules.yaml` and the host-backend installers (cron/launchd/systemd) are not
> yet built. The primitives this doc leans on *are* built and load-bearing already: `cairn run
> --idempotent` (§3, resume-or-skip on an existing run dir), per-run locking, and `cairn ps` as the
> morning-after view. The `--headless` gate defaults and the (still-designed) budgets/webhook-sink of
> §4 are covered in SECURITY/OBSERVABILITY. Everything schedule-specific below is the intended shape.

---

## 1. Declared schedules — `schedules.yaml`

Schedules are workspace data, committed and reviewed like everything else:

```yaml
# schedules.yaml
weekly-prospect-batch:
  cron: "0 3 * * 1"                       # Mondays 03:00
  run:  [batch, brease-rebuild, --params-file, prospects.jsonl, -j, "4",
         --to, blueprint, --gate, scope=all]

nightly-refresh:
  cron: "30 2 * * *"
  run:  [run, brease-rebuild, --param, url=https://client.example, --param, mode=rebuild,
         --headless, --idempotent]
```

`run:` is a cairn argv — schedules can invoke `run`, `batch`, `resume`, `gc`, or the
`self-improve` pipeline; nothing schedule-specific exists at the pipeline layer.

## 2. Installation — `cairn schedule`

```
cairn schedule install [--backend cron|launchd|systemd]   # sync schedules.yaml → host scheduler
cairn schedule list                                        # declared vs actually-installed diff
cairn schedule run <name>                                  # execute one entry NOW (also what the
                                                           # host timer calls — see below)
cairn schedule uninstall
```

**The indirection that keeps it maintainable:** the installed host entry is always the same one
line — `cairn schedule run <name>` — never the expanded argv. Editing `schedules.yaml` changes
behavior without touching crontabs; `install` is only needed when entries are added/removed or
timing changes. Installation renders per backend (a marker-fenced crontab block, launchd plists,
systemd user timers), is idempotent, and never touches entries outside its markers.

## 3. Idempotent invocation — the primitive that makes timers safe

`cairn run --idempotent`: resolve the `run_id` as normal (it already embeds `{date}`); **if that
run dir exists, resume it instead of creating a variant**; exit 0 immediately if it is already
complete. Consequences, all free:

- a re-fired timer (machine woke late, cron double-fire) is a no-op or a resume, never a duplicate;
- **catch-up after failure needs no backfill machinery** — the next firing resumes from the last
  valid artifact, because resume already works that way;
- overlap is safe — the run lock (`SECURITY.md` §5) makes the second invocation exit cleanly with
  "run is held".

## 4. Unattended safety — already designed, now load-bearing

Scheduled runs are headless runs; every protection they need exists and is simply *required* here:
`--headless` gate defaults (CMS populate defaults **no** — a schedule can never mutate a CMS
unattended), **budgets** with exit 7 (`SECURITY.md` §4) capping unattended spend, per-run locking,
and the **webhook sink + `gate-pending`/`run-halt` events** (`OBSERVABILITY.md`) so a halted or
human-blocked scheduled run notifies instead of silently rotting. `cairn ps` is the morning-after
view of everything the timers did.

## 5. Non-features, named

No daemon, no in-cairn queue, no distributed/multi-machine scheduling, no cron-expression
evaluation at runtime (the host evaluates time; cairn evaluates *state*), no missed-run backfill
beyond what `--idempotent` resume provides, and no schedule-level retry policies (a failed run
halts; the next firing resumes it — that *is* the retry policy).
