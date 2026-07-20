# cairn — Hardening & Remediation Plan

Derived from the 2026-07-14 three-model review panel (codex gpt-5.6-sol · grok · claude Fable 5);
full findings in `.orchestrate/reports/synthesis.md`. Every task below traces to a **code-verified**
finding and, where possible, to an executable spec in `tests/unit/test_panel_findings.py` (the
`xfail(strict=True)` specs flip to green when the fix lands — that flip **is** the definition of
done for those tasks).

This file is the plan input for `/orchestrate`. Read it top to bottom: §1 sequencing, §2 the
per-task specs, §3 cross-cutting conventions, §4 the orchestration recipe.

---

## 0. Prime directive & guardrails

- **Kernel stays dependency-light**: stdlib + `pyyaml` + `jsonschema` ONLY. No task may add a
  runtime dependency. If a fix seems to need one, escalate instead.
- **Downward-only dependency rule** (ARCHITECTURE §1) is invariant: executors never read pipelines;
  the kernel never contains a CLI name; all mutation lands in one run dir.
- **Fix the red baseline FIRST** (Task W0). `pytest tests/unit` is currently **54 failed / 771
  passed** on a clean tree — no other task's "tests pass" claim is trustworthy until this is green
  or the failures are explicitly quarantined with a documented reason.
- **Do not weaken any existing test to make a new one pass.** If a fix changes documented behavior,
  update the doc (`docs/ARCHITECTURE.md`, `API.md`, `SECURITY.md`) in the same task.
- **Every task updates docs it invalidates.** Several findings are "docs claim X, code does Y" — the
  fix is either to make code match docs or docs match code; state which, explicitly.

---

## 1. Sequencing (waves)

Waves are dependency-ordered. Within a wave, tasks touch disjoint files and are **parallel-safe**
(worktree fan-out). Across waves, complete and merge the earlier wave first (later waves build on
the typed-failure and validation primitives the earlier ones add).

| Wave | Theme | Tasks | Parallel-safe within wave? |
|---|---|---|---|
| **W0** | Unblock | `W0-baseline` | n/a (single) |
| **W1** | Failure taxonomy (highest ROI, low risk) | `W1-spawn-typed`, `W1-agent-exit`, `W1-run-status-finally` | Yes — but all touch `walk.py`/`base.py`; see §1.1 |
| **W2** | Control-plane integrity (security-critical) | `W2-gate-provenance`, `W2-shim-basename`, `W2-loop-cycle-guard` | Yes (gatekit / guards / plan — disjoint) |
| **W3** | Honest enforcement posture | `W3-guard-install-or-warn`, `W3-hook-layer-plan-warn` | Partially — both touch guards/plan; serialize |
| **W4** | Adapter isolation & robustness | `W4-claude-stdin`, `W4-config-isolation`, `W4-step-schema`, `W4-effort-max` | Yes (claude.py / all adapters / base.py / types.py) |
| **W5** | Drift & observability | `W5-doctor-drift`, `W5-record-versions`, `W5-node-timestamps`, `W5-capability-truth` | Yes (mostly disjoint) |
| **W6** | Lower-severity cleanups | `W6-symlink-contain`, `W6-guard-when`, `W6-fail-open-warn`, `W6-redact-multiline`, `W6-grok-flag-drift` | Yes |

### 1.1 W1 coordination note
`W1-spawn-typed` (base.py `run_process`) and `W1-agent-exit` + `W1-run-status-finally` (walk.py) can
run in parallel worktrees, but all three converge on the walker's failure path. **Integrate W1 as a
unit**: run the three worktrees, then one integrator merges and runs the full W1 acceptance set
together. Pin briefs with `@ <sha>` (worktree run).

---

## 2. Task specs

Each task: **Finding · Severity · Files · Change · Done-when · Risk**. "Done-when" is the exact
gate; where a `test_panel_findings.py` spec exists, dropping its `xfail` marker and seeing it pass is
mandatory.

### W0-baseline — VOID (corrected 2026-07-14)
- **Resolution**: the reported "54 failed / 771 passed red baseline" was a **controller
  test-invocation error**, not a real regression. Running bare `python -m pytest` used the system
  interpreter where `cairn-pipelines` is not installed, so `importlib.metadata` returned zero
  `cairn.executors` entry points and every executor-spawning test failed with
  `executor 'shell' has no registered plugin`. Run the project's way — **`uv run pytest tests/unit`**
  — the suite is **green: 825 passed, 6 xfailed** (the 6 are the panel-findings specs). No triage
  needed; there is nothing to fix here.
- **CANONICAL TEST COMMAND for this whole run**: `uv run pytest …` (never bare `python -m pytest`).
  Every implementer/reviewer brief must state this, or it will hit the same false-red.
- **Minor note (not blocking)**: under `uv run`, each executor entry point appears **twice** in the
  registry (`['claude','codex','grok','shell','stub','claude',…]`) — likely duplicate dist metadata
  in the venv. Harmless today (registry de-dups by name at use); worth a cleanup pass but out of scope
  for the hardening waves.

---

### W1-spawn-typed — typed executor spawn failures
- **Finding**: codex-F14 / claude-F4 (major, consensus). `subprocess.Popen` (`base.py:115`) unguarded;
  `FileNotFoundError`/`OSError` escapes the walker's `except CairnError` (`walk.py:426`) → raw
  traceback, `run.json` stuck `running`, gc can't reap.
- **Severity**: major.
- **Files**: `cairn/executors/base.py` (`run_process`), maybe `cairn/kernel/errors.py`.
- **Change**: in `run_process`, wrap the `Popen(...)` call; on `OSError` raise a `CairnError`
  subclass (reuse or add `ExecutorSpawnError(CairnError)` in `errors.py`) carrying the executable
  name and a safe diagnostic (no full env). The walker's existing `except CairnError` at walk.py:426
  then maps it to `ExitCode.EXECUTOR` (exit 4) with a `step-fail` event — no walker change needed for
  this half.
- **Done-when**: `test_spawn_failure_raises_typed_cairn_error` passes with its `xfail` marker
  **removed**. A run against a workspace whose `default_executor` binary is absent exits 4 with a
  `run-halt`/`step-fail` trail event (add an integration assertion in `tests/unit/test_walk.py` or a
  live test).
- **Risk**: low. Ensure the message is redactor-safe (don't dump env/argv secrets).

### W1-agent-exit — honor agent-step exit codes
- **Finding**: codex-F7 · grok-F11 · claude-F3 (major, consensus). `walk.py:446`
  `if step.kind == "run" and result.exit_code != 0:` gates exit-code only for shell steps; an agent
  CLI exiting non-zero (auth/API/policy failure) but leaving a valid artifact is recorded
  `step-done`, and on a retry path burns every retry.
- **Severity**: major.
- **Files**: `cairn/kernel/walk.py` (~line 446).
- **Change**: extend the exit-code check to **all** step kinds, not just `"run"`. A non-zero
  `result.exit_code` from any executor sets `ok = False` and appends
  `f"command exited with code {result.exit_code}"` to `validator_reasons`. **Additionally**: when the
  exit code (or captured output signature) indicates an executor failure rather than a content
  failure (reuse hookprobe's `_AUTH_SIGNS` classification), raise `_Halt(ExitCode.EXECUTOR, …)` and
  **skip remaining retries** — retrying an auth failure is pure waste. Distinguish: content-invalid →
  retry (exit 3 path); executor-failed → halt exit 4, no retry.
- **Done-when**: `test_agent_step_nonzero_exit_is_a_failure` passes with `xfail` removed (the source
  line no longer restricts to `kind == "run"`). Add a walker-level test: fake agent executor returns
  `Result(exit_code=1)` + a valid artifact + `retry:2` → asserts one invocation and exit 4 (not three
  invocations and exit 3).
- **Risk**: medium — must not regress the *content-invalid → retry* path (that path is load-bearing
  for the validator-feedback retry loop, ARCHITECTURE §3.1). Keep the two failure classes separate.

### W1-run-status-finally — no run left "running" on an escape
- **Finding**: claude-F4 (belt for W1-spawn-typed). Any non-`_Halt` escape from the walk leaves
  `run.json.status == "running"`, which gc (SECURITY §5) never collects.
- **Severity**: major.
- **Files**: `cairn/kernel/walk.py` (`_Walk.run` / top-level walk entry).
- **Change**: wrap the walk body so any exception that is not already a clean halt marks the run
  halted (with an `internal-error` reason and a distinct exit code, likely `EXECUTOR`) before
  re-raising, so `run.json` never stays `running` after the process dies.
- **Done-when**: a deliberately-injected unexpected exception during a step leaves `run.json.status`
  != `"running"` and a `run-halt` (or equivalent) trail event exists. New test in `test_walk.py`.
- **Risk**: low. Don't swallow the exception — record then re-raise.

---

### W2-gate-provenance — gate decisions can't be forged by an agent
- **Finding**: codex-F1 (critical). `gatekit.py:68` trusts any `gates/<name>.json` in the
  agent-writable run dir; an injected step writes `{"choice":"yes","by":"tty"}` and skips a
  production gate with no provenance/authorization/options check.
- **Severity**: critical (security).
- **Files**: `cairn/kernel/gatekit.py`, and whichever writer path is authoritative
  (`_commit`, `answer_gate`), plus the walker's gate resolution call site.
- **DECISION (locked 2026-07-14): approach A — authenticated decision file.** Rationale: cairn's
  core value is *executor-agnostic* containment. Approach B (relocate the control plane out of the
  sandbox) depends on each CLI's sandbox granularity — codex `--sandbox workspace-write` is coarse
  (whole cwd), so B either splits run state across two locations (breaking the "one run dir" audit/gc
  story) or forces produces under a `work/` subtree (a large, cross-cutting artifact-path change).
  Authentication works identically regardless of any executor's sandbox model, adds no state-splitting,
  and is the standard way to make a shared-filesystem control plane tamper-evident. It is the more
  reliable long-run choice (no per-CLI sandbox assumptions to drift).
- **Change (A — authenticated decision file)**: `_commit`/`answer_gate` write an HMAC-SHA256 over the
  canonical `{gate,choice,by,at}` payload, keyed by a **per-run secret generated at mint** and stored
  in a mode-0600 file **outside any agent-writable tree** (under the kernel's per-user/per-run control
  location, never inside the run dir the agent has cwd/write on). `resolve_gate` recomputes the MAC and
  **rejects** any decision file whose MAC is absent or invalid → treat as unanswered (re-ask when
  interactive, halt needs-human when headless) and emit a `gate-tamper` trail event. Also validate
  `choice ∈ gate.options` on read (defense in depth). **Reliability guard**: a missing/corrupt secret
  is treated as tamper (fail safe → the gate is unresolved, never auto-passed), never as "trust the
  file".
- **Done-when**: `test_gate_decision_file_is_trusted_without_provenance` is **inverted** (rename to
  `..._is_rejected_...`) and passes: a hand/agent-written decision file lacking valid provenance is
  NOT honored — the gate re-asks (interactive) or halts needs-human (headless). The legitimate
  `cairn gate <run> <name>=<choice>` path (`answer_gate`) still round-trips. Existing
  `test_gatekit.py` cases still pass (they use the legitimate writers, which now also stamp
  provenance).
- **Risk**: high-touch. The `answer_gate` external path and the interactive `_commit` path must both
  produce provenance the reader accepts, or you break the operator pattern (exit-6 → answer →
  resume). Add a test for that full loop. Keep the secret out of any file an agent can read.

### W2-shim-basename — reject absolute/traversing guard binaries
- **Finding**: codex-F4 (critical). `_binary_name` (`guards.py:187`) stops only on glob/space, so
  `/etc/cron.d/x *` → binary `/etc/cron.d/x`; `shim_dir / "/etc/cron.d/x"` (`guards.py:314`) collapses
  to the absolute path → arbitrary host-file write + chmod at plan time.
- **Severity**: critical (security).
- **Files**: `cairn/kernel/guards.py` (`_binary_name` and/or `build_shims`), plan-time validation in
  `cairn/kernel/plan.py` where guards are parsed (~line 1111).
- **Change**: require the derived binary to be a bare basename — reject any `match_command` whose
  binary contains `/`, `\`, a path separator, `.`/`..` segments, or is empty, with a `ConfigError`
  at **plan time** (fail before any file is written). In `build_shims`, additionally assert
  `(shim_dir / binary).resolve().parent == shim_dir.resolve()` as a defense-in-depth backstop.
- **Done-when**: `test_shim_build_rejects_absolute_or_traversing_binary` passes with `xfail` removed
  (build_shims raises and the victim file is untouched). Add a plan-time test: a pipeline with a guard
  `match_command: "/tmp/x *"` fails `cairn plan` with exit 2 naming the guard.
- **Risk**: low. Verify no legitimate guard fixture uses a slashed command (grep `match_command` in
  `templates/workspace/` and tests).

### W2-loop-cycle-guard — a loop produce must vary by {cycle}
- **Finding**: codex-F3 (critical). `_completed_cycles` (`walk.py:606`) is `while True` returning only
  on validation failure; a loop-body produce path omitting `{cycle}` renders identically every cycle →
  infinite validator spawns / resume hang.
- **Severity**: critical (availability).
- **Files**: `cairn/kernel/plan.py` (dataflow verification / loop-body parse; `_parse_step` with
  `in_loop=True` already exists ~line 666, and `_check_value_body` already knows `allow_cycle`).
- **Change**: at plan time, for every `StepNode` inside a `LoopNode` body, assert each declared
  `produces` path template **contains a `{cycle}` placeholder** (or another per-cycle-varying token).
  A cycle-invariant produce inside a loop → `ConfigError` naming the step and artifact. This is the
  mirror of the existing "`{cycle}` only valid inside a loop" check.
- **Done-when**: `test_loop_completion_precondition_is_unbounded` still passes (precondition proof),
  AND a new `test_plan.py` case: a loop whose body produces `report.json` (no `{cycle}`) fails
  `cairn plan` with exit 2. As a runtime backstop, also bound `_completed_cycles` by the loop's
  configured `max` so a mis-authored plan cannot hang even if the plan check is bypassed.
- **Risk**: low-medium. Confirm the brease art-review loop template (docs mention it) already varies
  by cycle — if a shipped template violates the new rule, fix the template in this task.

---

### W3-guard-install-or-warn — stop asserting enforcement that isn't installed
- **Finding**: claude-F1 · grok-F3 · codex-F2 (critical, consensus). `install_guards` is a no-op
  (`_cli.py:117`) while adapters run `--permission-mode bypassPermissions` / auto-approve, justified
  by hooks that are never installed. Plans with no `shim` guards run enforcement-free.
- **Severity**: critical (security posture / honesty).
- **Files**: `cairn/executors/_cli.py`, `cairn/executors/{claude,codex,grok}.py`,
  `cairn/kernel/hookprobe.py` (recipes already prove the mechanism), `docs/ARCHITECTURE.md` §4,
  `docs/SECURITY.md`.
- **DECISION (locked 2026-07-14): implement the claude hook now + honest warn fallback for all.**
  Rationale: the framework's entire containment story rests on the hook layer being real, and the
  mechanism is *already proven* by `hookprobe.py` on this machine — leaving it a no-op when it demonstrably
  works is the less reliable choice. Implement it for the primary executor (claude), and make the
  framework honest everywhere else via the plan-time enforcement-free warning + capability-flag truth
  (`W5-capability-truth`). Codex/grok native install is **probe-gated**: wire it only where
  `cairn doctor --probe-hooks` reports hook-primary on the target machine; otherwise a typed, loudly
  logged no-op — never a silent one. Do NOT assert `blocking_hooks=True` for an executor whose install
  is a no-op.
- **Change** (incremental split):
  - **Phase 1 (this plan, required)**: implement `ClaudeExecutor.install_guards` to write a
    PreToolUse deny-hook into `<run_dir>/.claude/settings.json` — `hookprobe.py`'s `ClaudeHookRecipe`
    already demonstrates the exact mechanism works under `bypassPermissions`. Wire the walker to call
    `install_guards` for hook-enforced guards. Codex/grok: implement if their recipe is proven on the
    dev machine (probe says all three are hook-primary), else leave a typed no-op that **downgrades
    loudly** (next bullet).
  - **Phase 1 fallback (required regardless)**: when a plan runs an executor with `bypassPermissions`
    AND no live pre-execution layer covers a security-relevant guard (or there are zero guards), emit
    a **plan-time WARNING** ("run is enforcement-free: no hook/shim layer active for <executor>") and,
    for security-critical guards specifically, a `ConfigError` unless the operator opts in.
- **Done-when**: for the claude executor, a live/faked step with a hook-enforced guard denying `rm`
  actually blocks the `rm` (new hookprobe-style test). Plans with zero guards emit the enforcement-free
  warning. `Capabilities` flags now match runtime reality (see `W5-capability-truth`). Docs §4 status
  block rewritten to state what actually installs.
- **Risk**: high. This is the most consequential and most invasive task. It depends on `W2` (gate
  integrity) conceptually but not in code. Consider running it as its own `adversarial`-reviewed
  sub-effort. Do NOT claim hooks are wired for codex/grok without a passing probe on the target
  machine.

### W3-hook-layer-plan-warn — validate `enforce:` layers, reject no-op configs
- **Finding**: codex-F13 · claude-F10. Layer names aren't validated; `enforce: [hook]` (no-op today),
  `enforce: [shimm]` (typo), or `[]` all plan clean and silently enforce nothing.
- **Severity**: major.
- **Files**: `cairn/kernel/plan.py` (~line 1111 guard parse).
- **Change**: validate `enforce` against the supported enum `{hook, shim, post}`; reject unknown
  members with `ConfigError`. Fail planning when the selected executor cannot provide at least one
  **effective** pre-execution layer for a guard (until `W3-guard-install-or-warn` lands, that means a
  guard needs `shim` to be effective; hook-only or post-only for a command guard → warn or error).
- **Done-when**: `cairn plan` on `enforce: [shimm]`, `enforce: [hook]`, and `enforce: []` each produce
  a plan-time diagnostic (new `test_plan.py` cases). Interacts with W3 above — sequence after it or
  coordinate the "effective layer" definition.
- **Risk**: low, but coupled to W3-guard-install; do them in the same worktree or serialize.

---

### W4-claude-stdin — deliver the envelope on stdin, not argv
- **Finding**: claude-F2/F6 (major/minor). `claude.py:21` passes the whole envelope as one argv arg →
  `ps`-readable + Linux `MAX_ARG_STRLEN` (128 KiB) `E2BIG` crash on skill-heavy steps. Codex already
  uses stdin.
- **Severity**: major.
- **Files**: `cairn/executors/claude.py` (`_build_command`), verify `_cli.py`'s stdin plumbing (it
  already supports codex's stdin path).
- **Change**: `claude -p` reads the prompt from stdin when the positional arg is absent — return
  `(argv_without_prompt, prompt_text)` from `_build_command`, mirroring the codex adapter.
- **Done-when**: `test_claude_prompt_is_passed_on_argv_not_stdin` is **inverted** (prompt NOT in argv,
  delivered via stdin) and passes. Existing `test_executors_claude.py` argv-pin updated. A 200 KiB
  envelope invokes without `E2BIG`.
- **Risk**: low. Confirm `claude -p` with no positional actually consumes stdin on the installed CLI
  (0.x) — verify against `claude --help` / a live smoke before pinning.

### W4-config-isolation — seal the "fresh" process from ambient user config
- **Finding**: codex-F6 · claude-F5 · grok-F6 (major, consensus). No adapter passes isolation flags;
  user hooks/MCP/memory/sandbox alter identical runs, breaking the determinism claim.
- **Severity**: major.
- **Files**: `cairn/executors/{claude,codex,grok}.py`.
- **Change** (per-CLI, verify each flag against the captured `--help`):
  - claude: add `--setting-sources project` + `--strict-mcp-config` (keeps run-dir hooks from W3,
    drops user/local sources).
  - codex: add `--ignore-user-config` and (if present) `--ignore-rules`; provide an isolated auth-only
    `CODEX_HOME` if feasible.
  - grok: add `--no-memory` (+ sandbox/plan flags as verified); relocate `GROK_HOME` per the
    hookprobe recipe's compat-off enumeration.
- **Done-when**: per adapter, a test asserts the isolation flag appears in argv; a live/faked step with
  a hostile user-level hook/MCP is unaffected. Document the sealed posture in ARCHITECTURE §5.
- **Risk**: medium. Isolation flags can drift or differ by version — gate each behind the doctor
  drift check (W5-doctor-drift) so a missing flag warns rather than hard-fails. Don't over-seal such
  that auth (which lives in user config for some CLIs) breaks — keep auth-only config reachable.

### W4-step-schema — validate the STEP block; robust sentinel framing
- **Finding**: codex-F10 · claude-F13 (major/minor). `parse_step_sentinel` (`base.py:71`) accepts any
  JSON object → wrong-shaped `{"learnings":["x"]}` crashes the walker with `AttributeError`; the
  non-greedy regex truncates on a `STEP>>>` substring inside the payload.
- **Severity**: major.
- **Files**: `cairn/executors/base.py` (`parse_step_sentinel`, `_STEP_RE`), the run's
  `step-return.schema.json` (already written to `.cairn/` and passed as `inv.return_schema`, currently
  unused by CLI executors).
- **Change**: (1) after extracting a block, validate it against the step-return schema before
  returning; a wrong-shaped block → treat as unparsable (return None, soft-fail per the authority
  rule) rather than handing it downstream. (2) Harden framing: on a truncated non-greedy match that
  fails `json.loads`, retry greedily / scan balanced-brace JSON from `<<<STEP`, OR (preferred, larger)
  move claude/grok to a structured `--output-format json` and read the sentinel from the framed result
  field. (3) Guard the walker's `learn.get(...)` against non-dict members regardless.
- **Done-when**: `test_step_sentinel_rejects_wrong_shaped_object` and
  `test_step_sentinel_survives_marker_in_payload` both pass with `xfail` removed. No walker traceback
  on a malformed block.
- **Risk**: low-medium. The authority rule (artifacts outrank STEP) must stay intact — a rejected STEP
  is a soft signal, never a hard fail here.

### W4-effort-max — add `max` to the effort enum
- **Finding**: claude-F11 (minor). `types.py:22` `EFFORTS` stops at `xhigh`; the claude CLI accepts
  `max`.
- **Severity**: minor.
- **Files**: `cairn/kernel/types.py` (`EFFORTS`), possibly per-executor effort mapping in adapters.
- **Change**: add `"max"` to `EFFORTS`. Map it per executor where the CLI lacks `max` (grok has no CLI
  effort flag; codex has `max`/`ultra` via `-c model_reasoning_effort`) — a tier can pin per-executor
  effort, so cross-CLI asymmetry is representable.
- **Done-when**: `test_effort_enum_includes_max` passes with `xfail` removed. A tier with
  `effort = "max"` plans without a ConfigError and the claude adapter emits `--effort max`.
- **Risk**: trivial. Ensure codex/grok mappings don't emit an invalid flag for `max`.

---

### W5-doctor-drift — doctor actually checks flags, models, auth
- **Finding**: codex-F19 · grok-F14 · claude-F15 (consensus). Doctor only substring-matches
  `--version` (`_cli.py:124/132`); removed flags, dead model slugs, changed framing surface only at
  the first paid run.
- **Severity**: major (DX / cost-safety).
- **Files**: `cairn/kernel/doctor.py`, `cairn/executors/_cli.py`, per-adapter capability metadata.
- **Change**: in doctor, run `<cli> --help` (and `codex exec --help` / `grok models` where relevant),
  assert every flag `_build_command` can emit is advertised, and warn on each configured tier model
  absent from the CLI's model list. Offline, cheap, per-machine. Optionally an opt-in authenticated
  canary invocation.
- **Done-when**: a faked CLI whose help lacks a flag the adapter emits (e.g. `--effort`) makes
  `cairn doctor` warn naming the flag; a tier pinned to a nonexistent model warns. New tests in
  `test_toolcheck.py`/a doctor test.
- **Risk**: medium. Parsing `--help` is brittle — match on flag tokens, tolerate formatting drift, and
  make failures warnings (not hard errors) unless the flag is load-bearing.

### W5-record-versions — run.json records executor versions + git rev
- **Finding**: codex-F16 (major; `cli.py:557` "Versions stay empty"). Docs (ARCHITECTURE §10) claim
  run.json records CLI versions and workspace git rev and warns on drift; it records neither.
- **Severity**: major.
- **Files**: `cairn/cli.py` (run mint ~line 557), resume drift path (`_pipeline_drift_guard` ~581).
- **Change**: at mint, record each executor's `--version` and the workspace git rev (+ dirty state).
  On resume, compare and warn on drift (respect `--force`), matching the existing pipeline-hash guard.
- **Done-when**: run.json contains non-empty executor versions + git rev; resume under a changed
  reported version warns naming both. New test.
- **Risk**: low. Probing `--version` at mint adds a small startup cost — acceptable, already done in
  doctor.

### W5-node-timestamps — real per-node transition times
- **Finding**: claude-F12 (minor). `walk.py:901` stamps every node with the walk's construction-time
  `now`; a long run records all nodes finishing at second zero.
- **Severity**: minor.
- **Files**: `cairn/kernel/walk.py` (`_set_status`).
- **Change**: use a real clock (`datetime.now(...)` or an injected per-event clock) in `_set_status`;
  keep `self.now` for path/template rendering where run-scoped determinism is intended.
- **Done-when**: a two-step pipeline with a slow first step records differing `at` values across nodes
  in run.json (new test). Trail and run.json agree.
- **Risk**: trivial. Do NOT change `self.now` used for artifact-path rendering (that determinism is
  load-bearing).

### W5-capability-truth — capability flags describe runtime reality
- **Finding**: grok-F2/F3, claude-F7/F8, codex-F5/F15. `Capabilities`/adapter metadata assert
  unimplemented features: grok `output_schema=True`/`blocking_hooks=True`, dead `session_capture`
  globs, unwired `network`/session capture.
- **Severity**: major (honesty; feeds W3 and doctor).
- **Files**: `cairn/executors/{claude,codex,grok}.py`, `cairn/kernel/types.py` (Capabilities),
  wherever `session_capture` is (not) consumed.
- **Change**: for each flag, either implement the feature or set the flag to its true value.
  Minimum: `output_schema=False` where `--json-schema` isn't wired; `blocking_hooks` follows W3's
  outcome (True only where install actually happens, else `None`/probe-driven); `session_capture`
  either implemented (copy the session into `logs/` — resolve project dir from cwd hash) or set to
  `None` and pass the CLI's no-persistence flag; `network` policy wired into `Invocation` or removed
  from parse.
- **Done-when**: no capability flag asserts a feature no code path provides (audit test that
  cross-checks each True flag against a consuming code path). Docs matrix (ARCHITECTURE §4) updated.
- **Risk**: medium; overlaps W3. Coordinate the `blocking_hooks` value with W3's install outcome.

---

### W6 — lower-severity cleanups (each independent, parallel-safe)
- **W6-symlink-contain** (codex-F11, major/security): `artifacts.py:170` resolves paths but doesn't
  reject symlinks escaping `run_dir`. **Change**: `resolve()` every concrete artifact path and require
  it stay under `run_dir`; reject escaping symlinks. **Done-when**: a run-local symlink to an external
  file is rejected by validation (new `test_artifacts.py` case).
- **W6-guard-when** (codex-F12, major/security): `guards.py:334` drops the runtime `when=` when shims
  reload guards, so a runtime-false guard still fires. **Change**: serialize the condition into the
  manifest and evaluate it in the shim-check entry (or evaluate active guards in the walker just
  before invocation). **Done-when**: a shim guard with a false `when` does not run its check (new
  test). *(Coordinate with W3/guards touches.)*
- **W6-fail-open-warn** (codex-F18, major/security): `guards.py:173` fail-open path prints its reason
  only when denied, so a crashed/timed-out guard silently allows with no diagnostic. **Change**: emit a
  structured guard-error event + write the fail-open reason to the step log before allowing.
  **Done-when**: a guard check exiting 1 with `on_error: allow` runs the command AND leaves an explicit
  fail-open warning in trail+log (new test).
- **W6-redact-multiline** (grok-F10, minor/security): `base.py:103` redactor is per-line; a secret
  split across lines leaks to log + STEP-parse input. **Change**: make redaction multi-line aware (or
  add a second whole-capture pass for the returned text). **Done-when**: a newline-split secret is
  absent from both `logs/<step>.log` and the captured text (new test).
- **W6-grok-flag-drift** (grok-F1, major/adapter): `grok.py:57` hardcodes `--no-auto-update`, absent
  from the current grok `--help`. **Change**: verify against the installed grok; gate the flag behind
  a version/help probe or remove it; ensure the doctor drift check (W5) covers it. **Done-when**:
  `test_executors_grok.py` argv pin reconciled with the installed CLI; doctor warns if the flag is
  unadvertised. *(Depends on W5-doctor-drift for the probe.)*

---

## 3. Cross-cutting conventions (all tasks)

- **Test-first where a spec exists**: the six `xfail(strict=True)` specs in
  `tests/unit/test_panel_findings.py` are the acceptance gates for W1-spawn-typed, W1-agent-exit,
  W2-shim-basename, W4-step-schema (×2), W4-effort-max. Removing the marker and seeing green is
  non-negotiable done. The three green tripwires
  (`test_gate_decision_file_is_trusted_without_provenance`, `..._loop_completion_...`,
  `test_claude_prompt_is_passed_on_argv_not_stdin`) must be **inverted** by their tasks (W2-gate,
  W2-loop, W4-claude-stdin) — flipping from "asserts the bug" to "asserts the fix".
- **Doc-sync is part of the task, not a follow-up.** Findings of the form "docs claim X, code does Y"
  (W3, W5-record-versions, W5-capability-truth) must land the code+doc change together.
- **No new runtime deps.** stdlib + pyyaml + jsonschema only.
- **Redactor safety**: any new diagnostic that includes argv/env/paths must pass through the existing
  redactor (SECURITY §1.3).
- **Every task adds at least one test** (unit preferred; live/faked where a real CLI is needed) and
  runs the full `pytest tests/unit` green (post-W0) before integration.

## 4. Orchestration recipe

Suggested invocation (staged for quality, worktrees for the parallel-safe waves):

```
/orchestrate docs/HARDENING-PLAN.md strategy=staged review=dual isolation=worktree \
    models=orchestrator:opus,worker:sonnet,reviewer:opus
```

- **W0 first, solo or single worker** — nothing else is trustworthy until the baseline is green.
- **W1 as a coordinated trio** (§1.1): three worktrees → one integrator merges + runs the joint W1
  acceptance set. Pin briefs `@ <sha>`.
- **W2 parallel** (gatekit / guards / plan are disjoint) — but `W2-gate-provenance` is high-touch and
  security-critical; give it a `panel:2` or `adversarial` review.
- **W3 as its own reviewed sub-effort** — most invasive; do not fan it out casually. `adversarial`
  planning on the install-vs-warn decision is warranted. Sequence W3-hook-layer-plan-warn after
  W3-guard-install.
- **W4, W5, W6 parallel worktrees** with `review=dual`.
- **Gate every merge**: spec review (does it meet the task's Done-when?) THEN quality review, in that
  order. Reviewers read the diff, not chat. A task that touches security (W2, W3, W6-guard/fail-open)
  gets the stronger reviewer model.
- **Ledger**: append each merged task to `.orchestrate/progress.md` with the finding id and the
  test that now passes.

## 5. Definition of done (whole plan)

1. `pytest tests/unit -q` is green (W0), and `test_panel_findings.py` reports `9 passed` (every
   `xfail` removed/inverted).
2. No `Capabilities` flag asserts an unimplemented feature (audit test).
3. `docs/ARCHITECTURE.md` §4 (guard matrix) and §10 (reproducibility), `docs/SECURITY.md` reflect the
   new reality — no aspirational claim without a code path.
4. A run against a missing binary exits 4 (not a traceback); an agent CLI exiting non-zero is exit 4,
   not a burned-retry exit 3; a forged gate file is rejected; a loop without `{cycle}` fails at plan
   time; the claude envelope rides stdin.
