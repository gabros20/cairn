# cairn — Observability & the Trail Protocol

How you watch a pipeline: the trail formalized as a **versioned, append-only event protocol** that
any logger, monitor client, or dashboard can build against — with zero servers, zero databases, and
zero new dependencies in the kernel.

**Design lineage (proven products, deliberately):** Bazel's Build Event Protocol (a typed event
stream *is* the build's public contract), Kafka ("the log is the API": monotonic offsets, consumers
own their position), Nextflow's weblog (push = a dumb webhook POST, no server), OpenTelemetry
(trace/span as an *export mapping*, not a kernel dependency), `docker events`/`kubectl --watch`
(streaming CLI UX). Rejected: Airflow's central metadata DB — a second state authority, the exact
disease cairn exists to avoid.

---

## 1. The Trail Protocol (v1)

One event stream per run: `runs/<id>/trail.jsonl`. Envelope:

```json
{ "v": 1, "seq": 42, "at": "2026-07-03T10:14:02.113Z", "run_id": "acme-redesign-20260703",
  "event": "step-done", "node": "capture", "attempt": 1, "cycle": null, "data": { … } }
```

**Guarantees** (what makes it a protocol, not a log file):
- **Single writer** — only the walker appends; executors/steps never touch it.
- **Atomic line appends, flushed per event** — a reader never sees a torn line as final.
- **`seq` strictly monotonic per run** — the consumer offset. A client that saw `seq: 42` resumes
  with `--since 42` and misses nothing, duplicates nothing.
- **Versioned envelope (`v`)** — additive changes (new fields, new event types) are non-breaking
  by contract; clients ignore unknown fields/events. Envelope shape changes bump `v`.
- **Append-only forever** — no rotation, no rewrite, no cleanup within a run. Runs are the
  retention unit (delete the dir).

**Event taxonomy (v1):**

| Category | Events | Key `data` fields |
|---|---|---|
| run | `run-start` · `run-done` · `run-halt` | params, dims, executors / totals / reason, exit_code |
| plan | `plan` | pipeline_hash, node list, matrix of resolved models |
| step | `step-start` · `step-done` · `step-fail` · `step-skip` · `retry` · `timeout` | model, log_path / artifacts, metrics, duration_s, **usage** (tokens/cost when the executor reports it) / validator_reasons |
| gate | `gate-pending` · `gate-answered` | question, options / choice, by |
| loop | `cycle-start` · `loop-capped` | cycle / residual summary |
| guard | `guard-deny` | guard name, command, reason |
| liveness | `heartbeat` *(optional)* | log_bytes, last_line |
| knowledge | `learn` | note, tag |

*Status: the run/plan/step/gate/loop/`learn` events above are emitted by the C1 walker today.
`guard-deny` lands with the guard engine (C3); `heartbeat` is opt-in config the walker parses but
does not yet emit (deferred — see IMPLEMENTATION-PLAN); `usage` fields populate once executors
report tokens/cost (C2+).*

Two events earn their keep specially: **`gate-pending`** turns the operator pattern from "poll and
infer" into "wait for the event" (an operating agent watches for it, asks the human, answers,
resumes). **`heartbeat`** (opt-in: `[defaults] heartbeat = "60s"`) makes a 90-minute build step
observable — emitted with the step-log byte offset so a client can incremental-read
`logs/<step>.log` without re-scanning; silence past two intervals = hung-before-timeout, visible
in any monitor.

## 2. Consuming — three tiers, cheapest first

### Tier 1 — the file (zero infra): *the log is the API*
`trail.jsonl` is tail-able by anything. The canonical reader:

```console
$ cairn trail <run-dir> --follow --json [--since SEQ]     # NDJSON to stdout, handles partial lines
$ cairn trail <run-dir> --follow --json | jq -r 'select(.event=="step-done") | .node'
```

Any logger or monitor client is `cairn trail --follow --json | <your program>` — or just reads the
file directly; the protocol guarantees make both safe. `--watch` (human TTY tree) renders from the
same stream.

### Tier 2 — sinks (push, no polling)
The already-sanctioned trail-sink plugin surface, now with two built-ins:

```toml
# cairn.toml
[sinks.webhook]                     # the Nextflow-weblog move: POST each event as NDJSON
url    = "https://ops.example.com/cairn"
events = ["run-*", "halt", "gate-pending", "guard-deny"]   # glob filter; default all

[sinks.jsonl]                       # default, always on — trail.jsonl itself is sink #0
```

Sinks are **tee — never authority**: fire-and-forget with bounded retry; a dead webhook cannot
slow or halt a run. Slack/desktop-notify/OTel are the documented plugin examples.

*Status: designed. Today `[sinks.jsonl]` (trail.jsonl itself, sink #0) is the only built sink; the
`[sinks.webhook]` push and the OTel exporter are post-C7 plugins — see IMPLEMENTATION-PLAN.*

### Tier 3 — OTel export (mapping, not dependency)
For fleets that live in Grafana/Datadog/Honeycomb: an **exporter plugin** maps the trail onto
OpenTelemetry semantics — run = trace (`trace_id` from `run_id`), node = span, retries/cycles =
child spans, `usage` = span attributes, `halt` = span error status. Two modes: live (a sink) or
post-hoc (`cairn export otel <run-dir>` — replays the trail; possible *because* the trail is
complete). The kernel never imports OTel; the mapping is specified so any exporter renders runs
identically.

## 3. Cross-run view — `cairn ps`

Batch and long-running fleets need "what's happening on this machine" without a daemon:

```console
$ cairn ps [--workspace .] [--json]
RUN                          STATUS    NODE        LAST EVENT      AGE
acme-redesign-20260703       running   build       heartbeat 12s   41m
globex-reimagine-20260703    gate      scope       gate-pending    2m   ← waiting on a human
initech-rebuild-20260702     halted    populate    guard-deny      3h
```

Implementation: scan `runs/*/run.json` + each trail's last line — no registry, no index file, no
lock manager. *Running vs crashed* is decided by heartbeat/event recency, not a PID file. `--json`
makes it the fleet API for the same monitor clients.

## 4. Where each question is answered

| Question | Answer surface |
|---|---|
| what is this run doing *right now*? | `cairn trail --follow` / last `heartbeat` |
| why did it stop? | `run-halt.data.reason` + `step-fail.validator_reasons` |
| what exactly did the model see? | `logs/<step>.prompt.md` (the envelope artifact) |
| what did the tool/model output? | `logs/<step>.log` (offset-indexed by heartbeats) |
| what did it cost? | `step-done.usage`, summed in `run-done.totals` |
| what's waiting on a human? | `gate-pending` events / `cairn ps` |
| what happened across the fleet? | `cairn ps --json` · Tier-2 webhook · Tier-3 OTel |
| what happened three weeks ago? | the run dir — the trail is the complete, replayable record |

## 5. Non-features, named

No metrics server, no `cairn serve` dashboard (a later *plugin* built on Tier 1/2 if ever), no
central run database, no log rotation inside runs, no sampling (pipelines emit tens of events, not
millions), and no OTel dependency in the kernel. The observability stack is: **a file with
guarantees, a follower, a teed webhook, and a documented mapping.** Everything above that is a
consumer, and consumers are other people's products.
