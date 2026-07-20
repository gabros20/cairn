# Event triggers — implementation plan (issue #3)

> **Status: PLAN — not built.** The design for `triggers.yaml`, the `cairn trigger` verb,
> and the poll-source cursor primitive. The event-side complement to `cairn schedule`:
> `schedule` : cron/launchd timers :: `trigger` : WatchPaths / systemd path-units.
> Origin: gabros20/cairn#3 (prospect-factory PRD evaluation — everything mapped onto
> existing primitives except inbound events).

## 0. Doctrine (what stays true)

- **No daemon, no listener.** cairn never owns a resident process; the host's own
  file-watch facility (launchd `WatchPaths`, systemd `.path` units) fires
  `cairn trigger run <name>`, exactly as the host clock fires `cairn schedule run <name>`.
- **An event is a file.** A JSON payload in a watched inbox directory, schema-validatable
  like any artifact. The filesystem stays the single authority.
- **The always-on part stays outside the kernel** (§4): a webhook bridge is a documented
  edge pattern, never a cairn feature. No TLS/auth surface, no new deps.

## 1. `triggers.yaml` — declared triggers

```yaml
# triggers.yaml (workspace root, sibling of schedules.yaml)
handle-reply:
  pipeline: handle-reply          # must exist in pipelines/ (checked at parse time)
  watch: inbox/replies/           # workspace-relative dir; one run per new file
  param: event                    # the claimed file's absolute path arrives as --param event=…
  # optional:
  glob: "*.json"                  # default "*" (dotfiles and subdirs always ignored)
  on_done: done                   # done → move to <watch>/.done/ (default) | delete
  on_fail: failed                 # move to <watch>/.failed/ (always move — never retry-loop a poison file)
```

Parse/validate mirrors `schedkit.load_schedules`: typed `Trigger` dataclass, loud
`ConfigError`s (unknown keys, unknown pipeline, absolute/escaping `watch:` path). Runs are
always `--headless` (a trigger can never block on a human — same rule as schedules) and
always `--idempotent`-safe by construction (see §2 claim semantics).

## 2. `cairn trigger run <name>` — the fired entry, at-most-once

The host watcher fires on *any* dir change and may coalesce or duplicate firings, so the
entry point owns dedupe, not the watcher:

1. Scan `<watch>/` for files matching `glob` (top-level only, skip dotfiles).
2. For each candidate, **claim by atomic rename** into `<watch>/.claim/<file>` —
   `rename(2)` is atomic on the same filesystem, so two concurrent firings can never both
   claim one file; losing the race (`FileNotFoundError`) means skip, not error.
3. Per claimed file: `cairn run <pipeline> --headless --param <param>=<claimed-path>`.
4. Exit 0 → move claim to `.done/` (or delete per `on_done`); nonzero → `.failed/`.
   The claim file is the at-most-once ledger; a crash mid-run leaves it in `.claim/`,
   surfaced by `cairn trigger list` as *stuck* (operator re-drops or discards — never
   auto-retried).

A run minted from an event embeds the event filename in its `run_id` template the normal
way (`{params.event}` is a path; authors use `run_id: "reply-{date}-…"` + the payload).

## 3. `cairn trigger sync` — install into the host watcher

Mirrors `schedule install` exactly: render-only functions + Runner-injected effects.

- **launchd** (primary target): one plist per trigger, `WatchPaths: [<abs watch dir>]`,
  `ProgramArguments: [<cairn-bin>, "trigger", "run", <name>, "--workspace", <ws>]`,
  label `io.cairn.trigger.<name>`. `ThrottleInterval: 10` — WatchPaths fires on every dir
  mutation including our own `.claim/` renames; the claim scan going empty makes the extra
  firing a cheap no-op. (`.claim/`/`.done/`/`.failed/` live *inside* the watched dir —
  dot-dirs are excluded from the scan, and the throttle absorbs the self-triggering.)
- **systemd**: `<name>.path` (`DirectoryNotEmpty=<watch>`) + `<name>.service` (oneshot,
  same argv). `DirectoryNotEmpty` re-fires while files remain — which is exactly the
  drain-the-inbox semantics §2 wants.
- **cron**: no file-watch facility — `sync --backend cron` REFUSES and prints the
  documented fallback (`schedules.yaml` polling the inbox every N minutes via
  `cairn trigger run <name>`, which is idempotent and cheap on empty).

Verbs: `sync` (install + prune removed triggers), `list [--json]` (declared vs installed
vs stuck claims), `remove <name>`. Same managed-block/label conventions as schedkit so the
two never collide.

## 4. Poll source with a persistent cursor (the one new kernel primitive)

The genuinely missing piece: per-entity watermark state that outlives runs and doesn't fit
the per-run dir. NOT a new node kind (the five-kinds contract holds) — a `cursor:` option
on `run:` steps:

```yaml
- id: poll-resend
  run: "scripts/poll-resend {cursor.value} {cursor.next} inbox/replies/"
  cursor: state/resend-cursor.json     # workspace-relative, kernel-managed
  produces: [poll-report]
```

Contract:
- `{cursor.value}` renders the current committed watermark (empty string on first run);
  `{cursor.next}` renders a kernel-chosen scratch path the step writes its candidate new
  watermark to.
- The kernel **commits `next` → the cursor file atomically only after the step's
  `produces` validate** — a failed/halted poll never advances the watermark, so events are
  re-fetched, deduped by the emit-to-inbox filename (writes are `O_EXCL`-create by event
  id: re-emitting an already-emitted event is a no-op, keeping poll → inbox at-least-once
  ⇒ inbox → run at-most-once via §2 claims).
- Cursor files live under workspace `state/` by convention, are plain JSON, and are
  `flock`ed during commit (two concurrent scheduled polls can't interleave).
- `cairn plan` validates the path (workspace-relative, no escape) and warns when a
  `cursor:` step isn't reachable from any schedule/trigger (a cursor with no clock or
  event feeding it is usually a mistake).

Paired with `schedules.yaml` (`*/5 * * * *` poll) this covers most "webhook" needs with
zero public surface — and upgrades to §3 triggers when the provider can push.

## 5. Webhook bridge — docs page only

`docs/TRIGGERS.md` ships with a **"Receiving webhooks the cairn way"** section: a
reference Cloudflare Worker (or tailscale-funnel + tiny handler) that verifies the
provider signature and drops the JSON payload into the inbox via rsync/object-store/git.
Explicitly NOT a kernel feature. Sequence mirrors SCHEDULING.md's diagram with the
provider in place of the clock.

## 6. Build order & tests

1. `kernel/triggerkit.py`: `load_triggers` + claim/consume engine — pure logic first,
   `tests/unit/test_triggerkit_load.py` / `_claims.py` (tmpdir inboxes, injected clock).
2. Renderers (`render_launchd_watch`, `render_systemd_path`) — render-only, snapshot
   tests like `test_schedkit_render.py`.
3. `sync`/`list`/`remove` with the injected `Runner` — effect tests like
   `test_schedkit_effects.py`; idempotency tests (double-sync = no-op) like
   `test_schedkit_idempotency.py`.
4. Cursor primitive in the walker (template roots `{cursor.*}`, commit-on-valid) —
   `test_walk` additions + a stub-executor pipeline in the scaffold's tests.
5. CLI verb + `docs/TRIGGERS.md` + scaffold example (`triggers.yaml` commented out, like
   the hello pipeline's agent step) + README/API §9 updates.
6. Live smoke on macOS launchd (this machine is the primary target per the issue), then
   flip the consumer PRD (brease-factory §6/§10) from scheduled-poll to trigger+bridge.

Acceptance = issue #3's three consumer criteria: declarative `watch:` → run with the file
path as a param under launchd WatchPaths; kernel-managed cursor surviving across runs with
dedupe; the bridge documented as an edge pattern.
