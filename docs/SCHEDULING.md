# cairn — Scheduling

First-class scheduled runs **without a scheduler**. cairn does not own a clock, run a daemon, or
manage a queue — the host already has a battle-tested scheduler (cron / launchd / systemd timers).
cairn's job is to make itself *perfectly schedulable*: declared schedules in the workspace,
one-command installation into the host, and idempotent invocations that a timer can fire blindly.
Lineage: `certbot renew`, `restic` + systemd timers, terraform-in-CI — the tool is
schedule-*ready*; the platform schedules.

> **Status: LIVE — built and tested.** `schedules.yaml`, the `cairn schedule`
> install/list/run/uninstall verb, all three host backends (cron/launchd/systemd installers +
> render-only unit generation), the declared-vs-installed diff, `schedule run`, and the
> `(pipeline, params, {date})` content-key idempotency (§3) are all shipped and covered by the
> unit suite. `cairn run --idempotent`, per-run locking, and `cairn ps` as the morning-after view
> back it. Still designed (not built): the budgets/exit-7 ceiling and the webhook sink of §4 —
> covered in SECURITY/OBSERVABILITY. Everything else below is the built behavior.

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
`self-improve` pipeline (`run self-improve`); nothing schedule-specific exists at the pipeline layer.

**Headless is enforced at load time.** `load_schedules` rejects a `run`/`batch`/`resume` schedule
whose `run:` argv omits `--headless` — a scheduled agent run can never block on a human, so the
requirement is checked (with a `ConfigError` naming the schedule) rather than trusted. `gc` is exempt
(it is inherently non-interactive). The `run:` argv is fired verbatim; `--idempotent` and `--headless`
are opt-in *there*, never injected by the scheduler.

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
systemd user timers), is idempotent, and never touches entries outside its markers. Target dirs
default to `~/Library/LaunchAgents` (launchd) and `~/.config/systemd/user` (systemd), overridable
with `--launchd-dir` / `--systemd-dir`.

**One cron shape the calendar backends refuse.** A cron expression that restricts **both**
day-of-month and day-of-week (e.g. `0 3 1 * 1` — "the 1st **or** a Monday") means a *union* in cron,
but launchd's `StartCalendarInterval` and systemd's `OnCalendar` can only **AND** their components.
Rather than silently install a schedule that fires on the wrong days, `render_launchd` /
`render_systemd` raise a `ConfigError` — use the `cron` backend (which passes the expression
verbatim), or split it into two schedules, one per day rule. (`@reboot` is likewise rejected — it
has no calendar time.)

## 3. Idempotent invocation — the primitive that makes timers safe

`cairn run --idempotent` matches by a **content key**, not a run-id string. The key is a sha256 over
the canonical `(pipeline, params, {date})` — the same identity the resolved `run_id` embeds
(`schedkit.find_idempotent_run` / `idempotency_key`). It scans the runs root for an equivalent run:

- a **complete** equivalent (`status == "done"`) → the firing is a **no-op**, exit 0;
- an **incomplete** equivalent → **resume that run dir** (never a variant), through the *same*
  pipeline-hash drift guard as `cairn resume` — a timer re-fire after a pipeline edit fails loud
  rather than silently resuming the old run against the new file;
- **none** → a fresh run.

Consequences, all free:

- a re-fired timer (machine woke late, cron double-fire) is a no-op or a resume, never a duplicate;
- **catch-up after failure needs no backfill machinery** — the next firing resumes from the last
  valid artifact, because resume already works that way;
- overlap is safe — the run lock (`SECURITY.md` §5) makes the second invocation exit cleanly with
  "run is held".

**The idempotency boundary is the calendar day.** The `{date}` bucket is `now` formatted `%Y%m%d`,
so dedup holds *within* a calendar day and the next day's firing gets a new key (a new run). One
consequence to know: a `Persistent=true`/catch-up firing (systemd, or a missed cron) that lands
*after* midnight computes tomorrow's key — so it is treated as a new run, not a duplicate of the
prior day's. `dims` are omitted from the key deliberately (they are derived from `params`).

## 4. Unattended safety — already designed, now load-bearing

Scheduled runs are headless runs; every protection they need exists and is simply *required* here:
`--headless` gate defaults (CMS populate defaults **no** — a schedule can never mutate a CMS
unattended), **budgets** with exit 7 (`SECURITY.md` §4) capping unattended spend, per-run locking,
and the **webhook sink + `gate-pending`/`run-halt` events** (`OBSERVABILITY.md`, still designed) so a
halted or human-blocked scheduled run notifies instead of silently rotting. `cairn ps` is the
morning-after view of everything the timers did.

For the cron path today, notification is built in without a sink: `cairn schedule run <name>`
captures the child cairn's stdout/stderr and **re-emits them verbatim after the child completes**, so
a halt reason and resume hint reach the host mailer (cron `MAILTO`) instead of being swallowed. The
child's stdout/stderr are surfaced as two post-hoc streams — child-side interleaving is not
preserved — and the child's exit code is propagated unchanged.

## 5. Non-features, named

No daemon, no in-cairn queue, no distributed/multi-machine scheduling, no cron-expression
evaluation at runtime (the host evaluates time; cairn evaluates *state*), no missed-run backfill
beyond what `--idempotent` resume provides, and no schedule-level retry policies (a failed run
halts; the next firing resumes it — that *is* the retry policy).
