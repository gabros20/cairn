# cairn — OS Filesystem Sandbox for the claude Executor: Design & Update Plan (C8 / W3c)

**Status:** proposed · **Finding:** C8 / W3c (deferred in `IMPLEMENTATION-PLAN.md`, `HARDENING-PLAN.md`) ·
**Author:** hardening follow-up, 2026-07-15 · **Companion to:** `SECURITY.md §2.2`, `ARCHITECTURE.md §4/§5`,
`GUARD-WHEN-PLAN.md`. **Grounded in:** a multi-front research sweep (Claude Code native sandboxing docs,
macOS Seatbelt / Linux bubblewrap+Landlock primitives, container & microVM landscape, and practitioner
signal on X incl. a live Claude Code sandbox-escape CVE) — sources inline in §3.

This document specifies how to close the **C8/W3c residual**: cairn's `claude` executor runs with
`--permission-mode bypassPermissions`, which grants the agent **unrestricted local filesystem read/write** —
there is no OS sandbox. It is written to be reviewed *before* implementation because it adds a new
security-critical layer beneath the executor boundary.

---

## 1. The problem, precisely

cairn delegates steps to coding-agent CLIs. Two of the three self-contain at the OS level; one does not:

| Executor | FS/OS containment today | Source |
|---|---|---|
| **codex** | `--sandbox workspace-write` + `sandbox_workspace_write.network_access=…` — OS sandbox, writes confined to cwd (= run dir) | `codex.py::_build_command` |
| **grok** | `--sandbox workspace` — single fs+network profile | `grok.py::_build_command` |
| **claude** | `--permission-mode bypassPermissions` — **no OS sandbox, full local FS r/w** | `claude.py:87` |

`claude` runs `bypassPermissions` deliberately: the default headless mode *refuses every tool use*
("I need your permission to write the file."), so cairn runs claude fully non-interactive and makes its
**blocking `PreToolUse` hook** (W3a) the tool gate (`claude.py:82-87`). The cost: **no filesystem boundary**.

### 1.1 What the missing boundary actually breaks (W3c)

Per the project memory (`cairn-bypasspermissions-fs-residual`) and `SECURITY.md §2.2`, under
`bypassPermissions` a fully-adversarial (or prompt-injected) claude agent can:

1. **Read the per-run HMAC secret** (in the gatekeys state dir, outside the run dir) → **forge a signed
   guard manifest** → defeat the W3a/C1 guard authentication.
2. **Rewrite `<run_dir>/.claude/settings.json`** → drop or neuter the PreToolUse hook.
3. **Read/write anything on the host** — `~/.ssh`, `~/.aws`, other repos, the user's files.

So today the honest posture is: for **codex**, guard inputs are genuinely tamper-proof (agent can't write
outside cwd); for **claude**, guards are **defense-in-depth with tamper-detection**, and the
**post-validator (artifact gate) is the only hard gate**. C8/W3c is the gap that makes that asymmetry true.

### 1.2 Scope of "solved"

The residual is specifically **filesystem** (the memory and SECURITY.md both frame it as FS r/w). Closing it
means: **confine claude's writes to `run_dir + workspace` (+ per-process temp), make everything else —
critically the gatekeys secret dir and the rest of the host — read-only or no-access.** That single change:

- Makes the W3a signed-manifest guarantee **real for claude** (secret becomes unreadable-for-write /
  unforgeable-in-place; the agent can't rewrite the manifest or steal-and-repurpose the secret outside
  its scope — see §6 for the exact reduction).
- Contains claude's **built-in file tools** (`Write`/`Edit`), not just its shell commands (§4, Decision 1).
- Brings claude to **parity with codex/grok** and lets cairn describe the claude hook as a real boundary.

**Network is out of primary scope** and treated separately (§5.4): the CLI *needs* network to reach its
model API, so "network off" is not a whole-process option without a domain-allow-list proxy — that is the
opt-in `strict` tier, not the default.

---

## 2. Threat model & the one hard truth from the field

**Attacker:** the claude agent process itself — adversarial via prompt injection or a compromised
tool/dependency — running headless under cairn with `bypassPermissions`. **Goal of the defense:** it cannot
write outside `run_dir + workspace`, cannot read host secrets (`~/.ssh`, `~/.aws`, …), cannot write the
gatekeys dir, and cannot reach the rest of the filesystem.

**The hard truth (X / CVE signal):** `CVE-2026-55607 — Claude Code Full Sandbox Escape` (writeup by
@v_metnew): *prompt injection → code execution on the host, "works even in read-only permissions mode + full
sandbox."* **Takeaway that shapes this design:** no single OS sandbox is fully trustworthy. Therefore:

- The OS FS sandbox is **added as a layer**, not as a new sole gate.
- **The `post` validator (artifact gate) remains the hard gate** (unchanged), and the W3a signed guards stay.
- The sandbox *shrinks* the attack surface and makes the file-based guards tamper-proof-in-practice for
  claude; it does not let us drop any existing check. Defense in depth, matching cairn's whole posture.

---

## 3. Landscape survey (condensed — full scorecards in the research appendix)

Five families were evaluated for **wrapping a per-step agent CLI, FS-scoped to `run_dir+workspace`, macOS
(Apple silicon) primary + Linux CI, no Python runtime deps, low per-invocation overhead**:

| Option | FS containment | macOS (mac-native binary) | Linux | Deps | Per-invocation cost | Verdict |
|---|---|---|---|---|---|---|
| **claude `sandbox.enabled`** (settings.json) | **Bash subprocesses only** — NOT the `Write`/`Edit`/`WebFetch`/MCP tools | native Seatbelt | bwrap+socat | none (in-claude) | ~ms | **Insufficient alone** (built-in file tools escape) — but a free *additional* layer |
| **`sandbox-exec` / Seatbelt (SBPL)** | whole process, kernel MAC | ✅ native (deprecated-CLI-but-works, macOS 15/26) | ✗ | none | ~ms | **macOS leg of choice** |
| **bubblewrap (`bwrap`) + Landlock** | whole process, mount+net ns | ✗ | ✅ (Flatpak-grade) | `bwrap` binary | ~ms | **Linux leg of choice** (Landlock/`landrun` fallback where unpriv userns is AppArmor-blocked) |
| **`@anthropic-ai/sandbox-runtime` (`srt`)** | whole process (wraps the two above) **+ network domain allow-list proxy** | ✅ (uses sandbox-exec) | ✅ (uses bwrap) | **Node.js** + ripgrep/socat | ~ms + proxy | **The `strict` opt-in tier** (adds egress control) |
| **containers (Docker/Podman)** | strong, but **cannot wrap a macOS-native binary** (Linux-VM only) | ✗ for mac-native claude | ✅ | daemon/VM | ~0.5–2s + virtiofs | Wrong tool on mac; heavy per-step |
| **microVMs** (Firecracker / Cloud Hypervisor / Apple `container` / gVisor) | strongest | Linux-guest only; Apple `container` (macOS 26) wraps *Linux* images, immature | ✅ | hypervisor | 100ms–seconds boot | Cloud/hosted-agent answer; overkill + wrong-guest for a local per-step mac-native wrap |

**Decisive facts:** (a) claude's own `sandbox.enabled` is **Bash-only** → the `Write`/`Edit` tools would
still escape, so we need a **whole-process** wrap (§4 Decision 1). (b) On macOS, **no container/microVM can
wrap the macOS-native `claude` binary** — only OS-native Seatbelt can. (c) `srt` is exactly a whole-process
Seatbelt+bwrap wrapper (it's what Claude Code's `/sandbox` is built on) but adds a **Node.js dependency** and
a network proxy cairn doesn't need for the FS fix.

Sources: Claude Code sandboxing docs (`code.claude.com/docs/en/sandboxing`, `.../sandbox-environments`,
`.../permission-modes`), Anthropic engineering "Making Claude Code more secure with sandboxing",
`github.com/anthropic-experimental/sandbox-runtime` (Apache-2.0, ~v0.0.65), `bwrap(1)` + `github.com/
containers/bubblewrap`, `landlock(7)` + `github.com/Zouuup/landrun`, Apple `sandbox-exec` deprecation
(`github.com/apple/containerization#737`), Docker/Podman macOS-VM docs, `CVE-2026-55607` writeup.

---

## 4. The load-bearing decisions

### Decision 1 — Whole-process wrap, **not** claude's Bash-only `sandbox.enabled`.
claude's native `sandbox.enabled` sandboxes only Bash subprocesses; its built-in `Write`/`Edit`/`WebFetch`
tools and MCP run **unconstrained on the host**. Since a claude step's file writes go through those built-in
tools, only wrapping the **claude process itself** contains them. `sandbox.enabled` is added as a cheap
*extra* in-claude layer (§5.5), never the primary.

### Decision 2 — a **pluggable `SandboxBackend` seam**: default = **cairn-owned** (sandbox-exec / bwrap); **`srt` a first-class optional backend**.
The design principle: **flexibility lives in the abstraction seam; maintainability lives in the default
behind it.** So the wrapper (§5.1) resolves an FS/net policy and hands it to a *backend* chosen by config
(`sandbox.backend = auto | native | srt | …`), and any backend — cairn-owned OS primitives today, `srt`, a
future microVM/`container` — is swappable without touching the executors or the policy. The **default
backend is cairn-owned**, because it is the lowest long-term maintenance liability *and* preserves cairn's
invariants:
- **No runtime dependency.** cairn is stdlib + pyyaml + jsonschema and *shells out*; driving `sandbox-exec`
  / `bwrap` (OS binaries already present — Seatbelt ships with macOS, `bwrap` is one package) keeps that.
  `srt` needs **Node.js** + ripgrep/socat — a new toolchain in every dev/CI image, on the security-critical
  path of every run.
- **Stable primitives vs. a moving beta.** SBPL and `bwrap` flags are decade-stable — "boring glue" written
  once. `srt` is Beta (v0.0.x, "config may churn"); making it the *required* boundary means tracking someone
  else's moving contract, pinning versions, and testing upgrades — a different, arguably worse, maintenance
  burden for a security boundary. When cairn's own backend breaks it is cairn's code, debuggable in-repo.
- **CLI-agnostic reuse.** The wrapper is a generic executor-boundary layer — it hardens **any** executor
  (claude today; defense-in-depth *beneath* codex/grok's own `--sandbox`; the honest home for cairn's
  `network: bool` across executors, today only codex-wired).
- **cairn owns its trust boundary.** The FS scope is derived from cairn's own `run_dir`/`workspace`/gatekeys
  paths, not delegated to a young external config surface.

**`srt` is a first-class optional backend, not a fork of the design.** Selecting `sandbox.backend = srt`
swaps the backend and *additionally* unlocks network egress control (its domain-allow-list proxy — the one
thing cairn can't cheaply build, §5.4). It earns its Node dependency exactly when a maintainer opts in — for
Anthropic-maintained isolation or egress control — and composes trivially because it *is* the same
primitives + a proxy behind the same seam. This gives the flexibility the maintainers asked for (pick your
backend per environment) while keeping the maintainable, dependency-free path as the default everyone gets.

### Decision 3 — Keep `bypassPermissions` + the PreToolUse hook as the **app-level tool gate**; the OS
sandbox is the **containment boundary**. (With a spike on `dontAsk`.)
The C8 gap is *filesystem containment*, which the OS sandbox closes orthogonally to permission mode. Keeping
`bypassPermissions` means the existing W3a hook stays the sole app-level gate — simplest, already proven by
`hookprobe`. Research surfaced a newer headless mode, **`dontAsk`** (deny-by-default, non-interactive, unlike
`bypassPermissions`), which would add an app-level backstop *even if the OS sandbox is absent*. It also adds
config surface (an explicit `--allowedTools` allow-list) and an unverified interaction with cairn's
PreToolUse hook + headless flow. **Plan:** ship the OS sandbox as the fix under the current
`bypassPermissions`+hook model; **spike `dontAsk`** as a follow-up belt-and-suspenders (§9 step 7) and adopt
it only if it composes without weakening the hook gate or the headless contract. *(This is the kind of
long-run-reliability fork the maintainer delegated — recommendation: OS sandbox now, `dontAsk` as a
measured follow-up, never a rushed swap of the working tool gate.)*

### Decision 4 — **`post` validator stays the hard gate; all W1–W6 + C9 layers unchanged.**
Per §2 (CVE), the sandbox is additive. No existing check is dropped or weakened.

---

## 5. Architecture — a CLI-agnostic `SandboxWrapper` at the executor boundary

### 5.1 Where it slots in
`CliExecutor.invoke` (`_cli.py:122`) calls `_build_command(inv) → (argv, stdin)` then `run_process(argv…)`.
The wrapper **prefixes** `argv` with the OS-sandbox launcher just before spawn:

```
argv, stdin = self._build_command(inv, prompt_text)
argv = self._sandbox.wrap(argv, inv, run_dir=inv.cwd, workspace=Path(inv.env["CAIRN_WORKSPACE"]))
run_process(argv, …)                       # unchanged
```

`wrap()` returns `argv` **unchanged** when the executor opts out or no sandbox primitive is available
(§5.6). The prompt-on-stdin contract, redaction, timeout, and `run_process` are all untouched.

**The backend seam (Decision 2).** `wrap()` computes an OS-neutral `SandboxPolicy`
(rw paths, ro paths, deny-default, net on/off) and dispatches to a `SandboxBackend` selected by
`sandbox.backend` config (`auto` → cairn-owned native by OS; `native`; `srt`; future `container`/`microvm`).
Each backend turns the policy into a launcher prefix (`NativeBackend` → sandbox-exec/bwrap; `SrtBackend` →
`srt --settings <generated>`). Executors and the policy never change when a backend is added or swapped —
that is where the flexibility lives.

### 5.2 Per-executor opt-in (a capability, honest by default)
Add `sandbox: str = "off"` to `Capabilities` (values `off` | `fs` | `strict`). Set **`claude` → `fs`**
(the fix). Leave **codex/grok → `off`** initially (they self-sandbox; the wrapper can later be enabled
*beneath* them as defense-in-depth once verified not to double-restrict their own sandbox). `shell`/`stub`
stay `off`. This keeps the change surgical and CLI-agnostic without silently altering codex/grok behavior.

### 5.3 The filesystem policy (identical intent on both OSes)
For a `fs`-posture executor, the generated profile grants:

| Path | Access | Why |
|---|---|---|
| `run_dir` (resolved, realpath) | **read-write** | the agent's workspace-of-record; artifacts, `.cairn/shims`, `.claude/settings.json` |
| `workspace` (resolved) | **read-write** | the repo the step edits |
| `TMPDIR` (per-process) | read-write | many tools need scratch |
| system dirs (`/usr`,`/bin`,`/lib*`,`/etc`,`/System`, dyld cache) | **read-only** | claude + python + git must load |
| **gatekeys dir** (`guard_manifests_dir()` + gate-keys, `XDG_STATE_HOME/cairn`) | **read-only** | the hook/shim subprocess must *read* the signed manifest + per-run secret to enforce — but must NOT write it (this is the W3c close, §6) |
| everything else (`~/.ssh`, `~/.aws`, other repos, `/`) | **deny** | the containment |

All dynamic paths are **realpath-resolved** before templating (symlink-widening guard — a symlink inside
the workspace pointing outward is resolved to its target and only allowed if the target is in-scope).

- **macOS:** an SBPL profile — `(deny default)`, `(allow file-read* …)` for system+gatekeys, `(allow
  file-write* (subpath RUN_DIR) (subpath WORKSPACE) (subpath TMPDIR))`, `(allow process-exec process-fork)`.
  Invoked `sandbox-exec -D RUN_DIR=… -D WORKSPACE=… -D TMPDIR=… -f <profile.sb> -- <argv>`. The deprecation
  WARNING on stderr is filtered from the captured log.
- **Linux:** `bwrap --die-with-parent --new-session --ro-bind /usr /usr … --ro-bind <gatekeys> <gatekeys>
  --bind <run_dir> <run_dir> --bind <workspace> <workspace> --proc /proc --dev /dev --tmpfs /tmp -- <argv>`.
  Fallback where unprivileged userns is AppArmor-blocked (Ubuntu 23.10+): **Landlock via `landrun`** (needs
  no userns), else degrade (§5.6).

### 5.4 Network (explicitly deferred to a `strict` tier)
The CLI needs network for its **model API** (`api.anthropic.com`), so a whole-process net-off wrap would
break the agent. Therefore the `fs` posture **leaves network as today** (on) — it closes the FS gap only,
which *is* the documented C8/W3c residual. Network **egress control** (allow `api.anthropic.com` + the
step's declared domains, deny the rest — which also stops `bash curl exfil`) requires a domain-allow-list
**proxy**; that is the **`strict`** posture, implemented by delegating to **`srt`** (which brings the proxy)
and mapping cairn's per-step `network: bool`/allowlist onto `srt`'s `allowedDomains`. `strict` is opt-in
(accepts the Node dep). The `fs` default ships first; `strict` is a follow-on (§9 step 7 / future).

### 5.5 Free extra layer — claude's own `sandbox.enabled`
cairn already writes `<run_dir>/.claude/settings.json` (read via `--setting-sources project`). Add a
`"sandbox": { "enabled": true, "failIfUnavailable": false }` block there so claude *also* self-sandboxes its
Bash subprocesses in-process. Bash-only (Decision 1), so it's belt-and-suspenders under the whole-process
wrap — but free and native. Gated so it never hard-fails a run where the primitive is missing.

### 5.6 Graceful degradation + `cairn doctor`
- **Probe:** `cairn doctor` gains a sandbox check per `fs`/`strict` executor — is `sandbox-exec` (mac) /
  `bwrap` or `landrun` (linux) present and functional (a tiny `--deny-all echo ok` smoke)? On Linux, detect
  the AppArmor-userns restriction and point at the fix (distro `bubblewrap` pkg / sysctl). WARN, never
  hard-fail doctor.
- **Missing primitive at run time:** fail **safe-and-loud** — emit a `sandbox-unavailable` warning to the
  trail/step log naming the executor and the missing binary, and (policy, §8) **either** proceed with the
  existing guards+post-validator as the boundary (default, preserves availability) **or** refuse the step
  under an opt-in `require_sandbox` strictness flag. Never silently run unsandboxed *without* the warning.

---

## 6. How this closes C8 **and** W3c (the exact reduction)

Under the `fs` sandbox, the gatekeys dir (secret + signed manifests) is **read-only** to claude and the rest
of the host is **deny/read-only**. Re-running the W3c attack list from §1.1:

1. **Forge a signed manifest** — needs to *write* a manifest the enforcement path will accept. Manifests
   live in the gatekeys dir → **read-only** → write denied by the kernel. The secret is *readable* (the hook
   must read it to compute the MAC) but the forged manifest has nowhere valid to land, and the enforcement
   path only reads from the protected path. **Closed** (reduced to "escape the OS sandbox", i.e. the CVE
   class, which the `post` gate still backstops).
2. **Rewrite `settings.json` to drop the hook** — `settings.json` is inside `run_dir` (writable), but claude
   reads it **once at startup**, before the agent acts; a mid-run rewrite doesn't affect the live process's
   hook. **Neutralized in practice.** (A stronger variant — relocate settings.json read-only outside run_dir
   — is noted as optional hardening; not required.)
3. **Read/write host files** (`~/.ssh`, other repos) — **denied** by deny-default FS scope. **Closed.**

Net: for claude, guard inputs move from *tamper-detectable* to *tamper-proof-in-practice* (equal to codex
under its sandbox), and the host FS is contained. The honest asymmetry in `SECURITY.md §2.2` goes away.

---

## 7. Interaction with existing layers (must all still work)

- **PreToolUse hook / shim guard chain.** The hook is a bash child of claude → runs *inside* the sandbox. It
  must read the signed manifest + per-run secret → the profile **ro-allows the gatekeys dir + XDG_STATE_HOME**
  (§5.3) and read+exec of the python/cairn install (system dirs). The shim dir is `<run_dir>/.cairn/shims`
  (inside the rw run_dir). Verified against `_build_env` (`walk.py:809` already passes `XDG_STATE_HOME`).
- **C9 per-invocation manifest.** Written by the walker to the gatekeys dir (ro to the agent, rw to cairn —
  cairn writes it *before* spawning the sandboxed claude). No conflict.
- **Network-dependent hook/shim.** Left on (§5.4) → subprocess network unaffected.
- **codex/grok.** Untouched (`sandbox: off` initially). No double-sandbox risk until explicitly enabled.
- **Reproducibility / redaction / timeout.** `run_process` and the redactor are unchanged; only `argv[0…]`
  gains a launcher prefix. The stderr deprecation-warning filter (mac) is the one new output nuance.

---

## 8. Edge cases & policy

- **Not-yet-existing artifact paths** under run_dir resolve fine (parent chain is in-scope) — mirrors W6's
  resolve-containment.
- **Symlink widening** — realpath-resolve every `-D`/bind path; a workspace symlink to `/etc` resolves out
  of scope → denied.
- **`require_sandbox` strictness** (opt-in, per §5.6): a config/CLI flag to *refuse* a `fs` step when no
  primitive is available, for high-assurance runs. Default off (availability > strictness), loudly warned.
- **macOS `sandbox-exec` tail risk** — deprecated CLI; abstraction seam (§5.1) lets the macOS leg swap to a
  future primitive (Apple `container`/Containerization when it can wrap a native process) without touching
  callers.
- **Parallel steps** — each invocation wraps its own argv with its own resolved run_dir/workspace; no shared
  state (mirrors C9's per-invocation isolation).

---

## 9. Implementation plan (files & sequence)

1. **`cairn/kernel/sandbox.py` (new).** The `SandboxWrapper` + the `SandboxBackend` seam (Decision 2):
   `wrap(argv, inv, *, run_dir, workspace) -> argv` builds an OS-neutral `SandboxPolicy` and dispatches to a
   backend chosen by `sandbox.backend` config. Ship **`NativeBackend`** (SBPL generation on mac +
   bwrap/landrun arg construction on linux; realpath resolution; `available()` probe) as the default; leave
   a `SrtBackend` stub/interface for the follow-on. Pure stdlib. No behavior when posture `off` or the
   backend is unavailable (returns argv unchanged + records a degradation reason).
2. **`cairn/kernel/types.py`.** `Capabilities.sandbox: str = "off"`.
3. **`cairn/executors/claude.py`.** `capabilities.sandbox = "fs"`; add the `sandbox.enabled` block to the
   settings.json writer (§5.5).
4. **`cairn/executors/_cli.py`.** In `invoke`, apply `self._sandbox.wrap(...)` for a `fs`/`strict` executor
   (constructed once per executor from `capabilities.sandbox`); filter the mac stderr deprecation line.
5. **`cairn/kernel/doctor.py` / `_cli.py`.** The sandbox availability + AppArmor-userns checks (§5.6), WARN.
6. **`cairn/kernel/walk.py`** (if needed). Surface a `sandbox-unavailable` trail warning + honor
   `require_sandbox` (fail-safe policy).
7. **(Follow-on)** `strict` tier via `srt` + the `dontAsk` spike (Decisions 2/3). Separate PR.
8. **Tests + docs** (§10, §11).

Each step is independently testable; the FS wrap (1–6) is the shippable unit; 7 is deliberately deferred.

---

## 10. Test plan (Done-when)

- **Containment (mac + linux, gated by primitive availability):** a `fs`-wrapped step that tries to write
  `$HOME/pwned` / read `~/.ssh/id_rsa` **fails**; writes under `run_dir`/`workspace` **succeed**. (Drive a
  `shell`/`stub` executor set to `fs` with a scripted command so the test needs no real claude.)
- **Gatekeys read-only:** inside the sandbox, reading the signed manifest + secret **works** (hook enforces);
  writing/forging a manifest into the gatekeys dir **fails** (kernel-denied) — the W3c reduction (§6).
- **Guard chain intact:** a hook/shim-guarded command is still denied through the sandbox (W3a tests pass
  wrapped); a normal artifact write still validates.
- **Degradation:** with the primitive stubbed absent, the step runs with a `sandbox-unavailable` warning
  (default policy) and, under `require_sandbox`, is refused — never silently unsandboxed-without-warning.
- **Opt-out parity:** codex/grok (`sandbox: off`) argv is byte-identical to today (no wrap) — regression
  guard.
- **Symlink widening:** a workspace symlink to an out-of-scope target is denied.
- **No-regression:** full suite green (`uv run pytest tests/unit -q`, 0 failed); the mac/linux-specific
  containment tests `skipif` the primitive is unavailable so CI on either OS stays green.

---

## 11. Docs to update on landing

- `SECURITY.md §2.2`: replace the "claude = defense-in-depth only, FS unrestricted" posture with "claude runs
  under an OS FS sandbox (`sandbox-exec`/`bwrap`), writes confined to `run_dir+workspace`, gatekeys dir
  read-only → guard inputs tamper-proof-in-practice"; keep the CVE-driven "no single sandbox is fully
  trusted; `post` is the hard gate" note; document the network-egress residual + the `strict`/`srt` opt-in.
- `ARCHITECTURE.md §4/§5`: the `SandboxWrapper` layer + the `Capabilities.sandbox` posture per executor.
- `IMPLEMENTATION-PLAN.md` / `HARDENING-PLAN.md`: move **C8/W3c** from deferred → in progress/done with a
  pointer here.
- Update the memory `cairn-bypasspermissions-fs-residual` once landed (the residual is closed for `fs`).

---

## 12. Risks & rollback

- **New security-critical layer.** Mitigation: additive and opt-in per executor (`sandbox: off` default for
  everything except claude); `post` gate + W3a guards unchanged; ship behind availability probes; extensive
  containment tests. Blast radius is the argv prefix + profile generation.
- **Platform variance / primitive absence.** Mitigation: fail safe-and-loud (§5.6); `skipif` OS-specific
  tests; doctor probes + the AppArmor-userns guidance baked in.
- **Perf.** `sandbox-exec`/`bwrap` add ~single-digit ms per invocation — negligible vs an agent step.
- **macOS CLI deprecation.** Mitigation: abstraction seam for a future swap (§8).
- **Rollback:** set `claude` `sandbox` back to `off` (one line) → exact pre-C8 behavior; the wrapper is inert
  when posture is `off`.

---

## 13. Recommendation

Proceed with a CLI-agnostic `SandboxWrapper` over a **pluggable `SandboxBackend` seam** — flexibility in the
seam, maintainability in the default. Ship the **cairn-owned `NativeBackend`** (`sandbox-exec` on macOS,
`bubblewrap`+Landlock on Linux) as the default, **claude → `fs`** first. It closes the documented C8/W3c
filesystem residual, makes claude's guard inputs tamper-proof-in-practice (parity with codex), adds **no
runtime dependency**, reuses across executors, and is safely gated + degradable. Keep `bypassPermissions`+hook
as the tool gate and the `post` validator as the hard gate (the CVE proves no OS sandbox is sufficient
alone). `srt` is a **first-class optional backend** (config-selected) that also unlocks **network egress
control**; it and the **`dontAsk`** app-backstop are measured follow-ons behind the same seam, not rushed
into the first landing.
