"""A CLI-agnostic OS **filesystem** sandbox for the coding-agent executors (C8 / W3c).

cairn's ``claude`` executor runs ``--permission-mode bypassPermissions`` — the agent has
whole-host filesystem read/write and there is no OS sandbox (``codex``/``grok`` self-contain via
their own ``--sandbox``; claude is the gap). This module closes that gap by **prefixing the
executor's argv with an OS-sandbox launcher** just before spawn, confining the wrapped process's
*writes* to ``run_dir + workspace`` (+ per-process temp), making the rest of the host read-only or
no-access — crucially the **gatekeys secret dir becomes read-only**, so a compromised claude can
read the per-run HMAC secret its own guard hook needs but can no longer *forge a manifest* there.
That single change makes claude's W3a signed-guard inputs tamper-proof-in-practice, parity with
codex under its write-sandbox (docs/CLAUDE-SANDBOX-PLAN.md §6).

**Design (docs/CLAUDE-SANDBOX-PLAN.md §5).** The wrapper resolves an OS-neutral
:class:`SandboxPolicy` (rw / ro / tmp paths, network on/off) and dispatches to a pluggable
:class:`SandboxBackend`. The default, cairn-owned :class:`NativeBackend` drives OS primitives that
are already present — macOS ``sandbox-exec`` (Seatbelt/SBPL), Linux ``bwrap`` (bubblewrap) with a
``landrun`` (Landlock) fallback. **No new runtime dependency**: pure stdlib here; the sandbox is an
external OS binary cairn execs. A future ``SrtBackend`` (``@anthropic-ai/sandbox-runtime``, which
also brings network egress control) slots in behind the same seam — see the NOTE at the bottom.

**Load-bearing invariants** (docs/CLAUDE-SANDBOX-PLAN.md §2, §7):

1. **Network stays ON.** The CLI needs its own model API (``api.anthropic.com``); the ``fs`` posture
   confines the FILESYSTEM only. macOS profile allows ``network*``; Linux does NOT ``--unshare-net``.
   Network egress control is the deferred ``strict``/``srt`` tier.
2. **The guard hook/shim subprocess still works inside the sandbox.** claude's PreToolUse hook and
   the PATH shims are children of the wrapped process; they exec ``python -m cairn.kernel.guards``
   and must read the signed manifest + per-run secret. So the policy ro-allows the gatekeys state
   dir + the interpreter prefixes + the cairn source root, and allows process-exec/fork.
3. **Gatekeys dir is READ-only, not writable** — the agent reads the secret (the hook needs it) but
   cannot write/forge a manifest there (the W3c close).
4. **Additive + gated.** The wrap is inert (returns argv unchanged) when the posture is ``off`` or
   the primitive is unavailable (loud-not-silent degradation). ``off`` executors (codex/grok/shell/
   stub) get byte-identical argv to today.

All dynamic paths are **realpath-resolved** (``Path.resolve()``) before templating — a symlink
inside the workspace pointing outward resolves to its real target and is only in-scope if the
target is (symlink-widening guard, mirrors W6 artifacts resolve-containment). The kernel enforces
against real paths too (macOS ``/tmp`` → ``/private/tmp``), so resolution also makes the grant
match what the kernel checks.
"""

from __future__ import annotations

import platform
import shutil
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from cairn.kernel import gatekeys

# Postures (values of ``Capabilities.sandbox``). ``off`` → inert passthrough; ``fs`` → the OS FS
# sandbox this module builds; ``strict`` → the deferred egress-controlling tier (treated as ``fs``
# for filesystem containment today, see the SrtBackend NOTE).
POSTURE_OFF = "off"
POSTURE_FS = "fs"
POSTURE_STRICT = "strict"
_ACTIVE_POSTURES = frozenset({POSTURE_FS, POSTURE_STRICT})


class SandboxUnavailable(Exception):
    """A backend could not confine this argv (primitive missing/failed to compile the policy).

    Raised by :meth:`SandboxBackend.wrap`; the :class:`SandboxWrapper` catches it, records the
    reason for a one-time ``sandbox-unavailable`` warning, and returns the argv UNCHANGED so the
    step still runs (availability > strictness — the default degradation policy). It never
    propagates into the run.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _resolve(p: Path | str) -> Path:
    """``Path.resolve()`` without requiring the path to exist (``strict=False`` is the default on
    3.6+). Realpath-resolving every policy path is the symlink-widening guard."""
    return Path(p).resolve()


def _dedup(paths: list[Path]) -> tuple[Path, ...]:
    """Resolve + de-duplicate, preserving first-seen order (deterministic profile output)."""
    seen: dict[Path, None] = {}
    for p in paths:
        seen.setdefault(_resolve(p), None)
    return tuple(seen)


# --------------------------------------------------------------------------- #
# SandboxPolicy — the OS-neutral description a backend turns into a launcher
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SandboxPolicy:
    """An OS-neutral filesystem+network policy. Backends translate it to a launcher prefix.

    ``rw_paths`` are read-write, ``ro_paths`` read-only, ``tmp_paths`` read-write scratch (kept a
    separate field so a backend can special-case per-process temp — bwrap can ``--tmpfs`` it). All
    paths are realpath-resolved and de-duplicated at :meth:`build` time. ``network`` gates whether
    the wrapped process may open sockets (kept True for the ``fs`` posture — invariant 1).
    ``deny_default`` is the containment stance: everything not explicitly allowed is denied.
    """

    rw_paths: tuple[Path, ...]
    ro_paths: tuple[Path, ...]
    tmp_paths: tuple[Path, ...]
    network: bool
    deny_default: bool = True

    @classmethod
    def build(
        cls,
        *,
        rw_paths: list[Path],
        ro_paths: list[Path],
        tmp_paths: list[Path],
        network: bool,
        deny_default: bool = True,
    ) -> SandboxPolicy:
        return cls(
            rw_paths=_dedup(rw_paths),
            ro_paths=_dedup(ro_paths),
            tmp_paths=_dedup(tmp_paths),
            network=network,
            deny_default=deny_default,
        )


# --------------------------------------------------------------------------- #
# SandboxBackend seam + the cairn-owned NativeBackend
# --------------------------------------------------------------------------- #


class SandboxBackend(ABC):
    """Turns a :class:`SandboxPolicy` into a launcher-prefixed argv. The abstraction seam where
    backend flexibility lives (cairn-owned native today; ``srt``/microVM later) — executors and the
    policy never change when a backend is added or swapped (docs/CLAUDE-SANDBOX-PLAN.md §5.1)."""

    @abstractmethod
    def available(self) -> bool:
        """Is the primitive present AND functional (a real confine-a-trivial-command probe)?
        Cached per process — a machine's sandbox availability doesn't change mid-run."""

    @abstractmethod
    def wrap(self, argv: list[str], policy: SandboxPolicy) -> list[str]:
        """Return ``argv`` prefixed with the OS-sandbox launcher, or raise
        :class:`SandboxUnavailable` if it cannot (the caller degrades)."""


def _sbpl_string(path: Path) -> str:
    """Quote an absolute path as an SBPL string literal. SBPL string syntax is C-like: wrap in
    double quotes, backslash-escape ``\\`` and ``"`` (paths with either are rare but must not break
    the profile or, worse, widen it)."""
    escaped = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class NativeBackend(SandboxBackend):
    """The default backend: OS-native primitives, no runtime dependency.

    macOS → ``sandbox-exec`` with an inline (``-p``) SBPL profile (Seatbelt, kernel MAC). Linux →
    ``bwrap`` (bubblewrap mount+pid namespaces) with a ``landrun`` (Landlock) fallback where
    unprivileged user namespaces are AppArmor-blocked (Ubuntu 23.10+/24.04). Both keep network on
    for the ``fs`` posture (invariant 1).
    """

    # macOS system read roots layered ON TOP of ``(import "system.sb")`` (which already grants the
    # dyld cache + libraries a process needs to *start*; without it a deny-default profile SIGABRTs
    # before main). These broaden reads to system locations tools load from — Homebrew, frameworks,
    # config — WITHOUT opening user files ($HOME stays denied). Existence-filtered at profile time.
    _MAC_SYSTEM_RO: tuple[str, ...] = (
        "/usr", "/bin", "/sbin", "/System", "/Library",
        "/private/var/db/dyld", "/etc", "/opt/homebrew", "/opt/local", "/usr/local",
    )
    # Linux system read roots bound read-only into the namespace. ``/lib`` and ``/lib64`` are
    # symlinks into ``/usr`` on merged-usr distros — bound only where they are real dirs.
    _LINUX_SYSTEM_RO: tuple[str, ...] = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")

    # The macOS deprecation line ``sandbox-exec`` MAY print to stderr on spawn on some OS versions
    # (absent on macOS 15/26 as probed). It does not corrupt the STEP-sentinel parse (that scans
    # for ``<<<STEP`` and json-raw-decodes, immune to a leading noise line) — kept here so a caller
    # that wants a pristine log can filter exactly this line. NOTE: run_process merges stderr into
    # stdout, so cairn does NOT rewrite it inline (that would risk the shared runner's byte-identity
    # invariant); the residual is one cosmetic log line on affected OSes, documented in SECURITY.md.
    MAC_DEPRECATION_NOISE = "sandbox-exec is deprecated"

    def __init__(self) -> None:
        self._os = platform.system()  # "Darwin" | "Linux" | ...
        self._available: bool | None = None
        self._launcher: str | None = None  # absolute path to sandbox-exec / bwrap
        self._linux_landrun = False  # True → available() fell back to landrun

    # -- availability ------------------------------------------------------- #

    def available(self) -> bool:
        if self._available is None:
            self._available = self._probe()
        return self._available

    def _probe(self) -> bool:
        if self._os == "Darwin":
            exe = shutil.which("sandbox-exec")
            if exe and self._probe_run([exe, "-p", self._mac_probe_profile(), "/usr/bin/true"]):
                self._launcher = exe
                return True
            return False
        if self._os == "Linux":
            exe = shutil.which("bwrap")
            if exe and self._probe_run(
                [exe, "--die-with-parent", "--ro-bind", "/usr", "/usr",
                 "--proc", "/proc", "--dev", "/dev", "--", "/bin/true"]
            ):
                self._launcher = exe
                return True
            # AppArmor-userns fallback (Ubuntu 23.10+/24.04 unprivileged-userns block): Landlock via
            # landrun needs no user namespace.
            land = shutil.which("landrun")
            if land and self._probe_run([land, "--rox", "/usr", "--", "/bin/true"]):
                self._launcher = land
                self._linux_landrun = True
                return True
            return False
        return False  # unsupported OS → degrade

    @staticmethod
    def _probe_run(argv: list[str]) -> bool:
        """Run a trivial confined command and report exit 0. Any spawn error / non-zero → not
        functional. Stdlib subprocess (not run_process — this is a machine probe, no logging)."""
        import subprocess

        try:
            proc = subprocess.run(argv, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    @staticmethod
    def _mac_probe_profile() -> str:
        return (
            '(version 1)(import "system.sb")(deny default)'
            "(allow process-exec* process-fork sysctl-read mach-lookup file-read-metadata)"
            "(deny network*)"
        )

    # -- wrap --------------------------------------------------------------- #

    def wrap(self, argv: list[str], policy: SandboxPolicy) -> list[str]:
        if not self.available() or self._launcher is None:
            raise SandboxUnavailable(self._unavailable_reason())
        if self._os == "Darwin":
            return [self._launcher, "-p", self.mac_profile(policy), *argv]
        if self._os == "Linux":
            if self._linux_landrun:
                return self.linux_landrun_argv(self._launcher, argv, policy)
            return self.linux_bwrap_argv(self._launcher, argv, policy)
        raise SandboxUnavailable(self._unavailable_reason())

    def _unavailable_reason(self) -> str:
        if self._os == "Darwin":
            return "sandbox-exec not available or failed to confine a probe command"
        if self._os == "Linux":
            return (
                "bwrap/landrun not available or failed to create a namespace — on Ubuntu 23.10+/"
                "24.04 unprivileged user namespaces may be AppArmor-restricted; install the distro "
                "`bubblewrap` package or `landrun`, or allow unprivileged userns "
                "(sysctl kernel.apparmor_restrict_unprivileged_userns=0)"
            )
        return f"no OS filesystem sandbox for platform {self._os!r}"

    # -- profile / argv generation (pure strings; unit-tested on any OS) ---- #

    def mac_profile(self, policy: SandboxPolicy) -> str:
        """Generate the SBPL profile string for ``policy`` (macOS). Pure — no I/O — so it is
        unit-testable on any OS. ``(import "system.sb")`` supplies the base system reads a process
        needs to start; ``(deny default)`` is the containment; explicit allows re-open exactly the
        policy's rw/ro/tmp scope and network."""
        lines = [
            "(version 1)",
            '(import "system.sb")',
            "(deny default)" if policy.deny_default else "(allow default)",
            # Exec children (the guard hook/shim run python), fork, and the metadata/lookup calls
            # loaders and tools make. file-read-metadata is unfiltered (stat is not content — it
            # does not expose ~/.ssh key BYTES, only their existence), matching the plan's §5.3.
            "(allow process-exec* process-fork sysctl-read mach-lookup file-read-metadata)",
        ]
        ro = [Path(p) for p in self._MAC_SYSTEM_RO if Path(p).exists()]
        ro += list(policy.ro_paths)
        for p in _dedup(ro):
            lines.append(f"(allow file-read* (subpath {_sbpl_string(p)}))")
        for p in _dedup(list(policy.rw_paths) + list(policy.tmp_paths)):
            lines.append(f"(allow file-read* file-write* (subpath {_sbpl_string(p)}))")
        lines.append("(allow network*)" if policy.network else "(deny network*)")
        return "\n".join(lines) + "\n"

    def linux_bwrap_argv(self, launcher: str, argv: list[str], policy: SandboxPolicy) -> list[str]:
        """Construct the ``bwrap`` argv for ``policy`` (Linux). Pure — unit-testable on any OS.

        Network stays shared (no ``--unshare-net`` — invariant 1). Env is NOT cleared
        (``--clearenv`` would drop cairn's curated PATH-with-shim-dir / XDG_STATE_HOME / CAIRN_*;
        run_process already passes exactly ``inv.env``). ``/lib`` and ``/lib64`` are bound only
        where they are real dirs (merged-usr symlinks would be redundant with ``/usr``).
        """
        out = [launcher, "--die-with-parent", "--new-session"]
        for d in self._LINUX_SYSTEM_RO:
            p = Path(d)
            if not p.exists() or p.is_symlink():
                continue  # skip merged-usr symlinks (/lib, /lib64) and absent dirs
            out += ["--ro-bind", d, d]
        for p in policy.ro_paths:
            out += ["--ro-bind-try", str(p), str(p)]
        out += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
        for p in list(policy.rw_paths) + list(policy.tmp_paths):
            out += ["--bind", str(p), str(p)]
        if policy.rw_paths:
            out += ["--chdir", str(policy.rw_paths[0])]  # run_dir is passed first
        out += ["--", *argv]
        return out

    def linux_landrun_argv(self, launcher: str, argv: list[str], policy: SandboxPolicy) -> list[str]:
        """Construct the ``landrun`` (Landlock) fallback argv. Pure — unit-testable on any OS.
        Landlock needs no user namespace, so it survives the AppArmor-userns block. ``--rox`` =
        read+execute, ``--ro`` = read-only, ``--rwx`` = read-write-execute. Network is left alone
        (Landlock is filesystem-only; it does not touch the net namespace — invariant 1)."""
        out = [launcher]
        for d in self._LINUX_SYSTEM_RO:
            p = Path(d)
            if p.exists() and not p.is_symlink():
                out += ["--rox", d]
        for p in policy.ro_paths:
            out += ["--ro", str(p)]
        for p in list(policy.rw_paths) + list(policy.tmp_paths):
            out += ["--rwx", str(p)]
        out += ["--", *argv]
        return out


# --------------------------------------------------------------------------- #
# SandboxWrapper — builds the policy from an invocation and dispatches
# --------------------------------------------------------------------------- #


def _state_cairn_dir(env: dict[str, str]) -> Path:
    """The ``.../cairn`` state dir that holds BOTH ``gate-keys`` and ``guard-manifests`` (siblings),
    resolved from the CHILD's env (the guard hook/shim inherit ``env`` and resolve the gatekeys dir
    from it — so ro-allowing must use the same ladder, not ``os.environ``). Mirrors
    :func:`gatekeys.gate_keys_dir`'s location ladder, one level up."""
    xdg = (env.get("XDG_STATE_HOME") or "").strip()
    if xdg:
        return Path(xdg) / "cairn"
    home = (env.get("HOME") or "").strip()
    if home:
        return Path(home) / ".local" / "state" / "cairn"
    return Path.home() / ".cairn"


def _interpreter_ro_paths() -> list[Path]:
    """Read-only roots the guard hook/shim child needs to exec python and import cairn INSIDE the
    sandbox (invariant 2): the venv prefix, the base interpreter install (uv/pyenv keep it outside
    the venv — e.g. ``~/.local/share/uv/python/...``), and the cairn source root (editable installs
    live outside site-packages). In a wheel install these collapse under ``sys.prefix``; listing all
    is cheap and covers dev/CI too."""
    roots = [Path(sys.prefix), Path(sys.base_prefix)]
    # cairn/kernel/sandbox.py → parents[2] is the dir CONTAINING the ``cairn`` package (repo root
    # for an editable install, site-packages otherwise) — what ``-m cairn.kernel.guards`` reads.
    roots.append(Path(__file__).resolve().parents[2])
    return roots


class SandboxWrapper:
    """Applies (or no-ops) the OS FS sandbox for one executor, per its ``Capabilities.sandbox``
    posture. Constructed once per executor and cached; ``wrap`` is called per invocation.

    When the posture is ``off`` the wrap is a pure passthrough (codex/grok/shell/stub argv is
    byte-identical to pre-C8). When the posture is ``fs``/``strict`` but the primitive is
    unavailable, ``wrap`` returns the argv UNCHANGED and records a one-time ``sandbox-unavailable``
    reason for the caller to surface (loud-not-silent degradation — never fails the run by default).
    """

    def __init__(self, posture: str, backend: SandboxBackend | None) -> None:
        self.posture = posture
        self.backend = backend
        self._pending_warning: str | None = None
        self._warned: set[str] = set()

    def wrap(
        self, argv: list[str], inv, *, run_dir: Path, workspace: Path
    ) -> list[str]:
        """Return ``argv`` prefixed with the sandbox launcher, or unchanged (off / degraded)."""
        if self.posture not in _ACTIVE_POSTURES:
            return argv
        if self.backend is None or not self.backend.available():
            self._degrade(
                "sandbox posture is "
                f"{self.posture!r} but no OS filesystem sandbox primitive is available"
            )
            return argv
        policy = self._build_policy(inv, run_dir, workspace)
        try:
            return self.backend.wrap(argv, policy)
        except SandboxUnavailable as exc:
            self._degrade(exc.reason)
            return argv

    def _build_policy(self, inv, run_dir: Path, workspace: Path) -> SandboxPolicy:
        env = getattr(inv, "env", {}) or {}
        rw = [Path(run_dir), Path(workspace)]
        tmpdir = (env.get("TMPDIR") or "").strip()
        if not tmpdir:
            import tempfile

            tmpdir = tempfile.gettempdir()
        tmp = [Path(tmpdir)]
        # ro: the gatekeys state dir (read the secret+manifest, but deny-default blocks WRITES there
        # — the W3c close), the interpreter + cairn source (so the hook/shim child runs), and a
        # distinct XDG_STATE_HOME/cairn if the env points elsewhere than the default ladder.
        ro = [_state_cairn_dir(env)]
        ro += _interpreter_ro_paths()
        # Network stays ON for fs (invariant 1): the CLI's own model API needs it; egress control is
        # the deferred strict/srt tier. inv.network governs codex today and is intentionally NOT
        # mapped onto the FS posture yet.
        return SandboxPolicy.build(rw_paths=rw, ro_paths=ro, tmp_paths=tmp, network=True)

    def _degrade(self, reason: str) -> None:
        self._pending_warning = reason

    def take_warning(self) -> str | None:
        """Consume the pending degradation reason ONCE (dedup across steps): returns the reason the
        caller should surface as a ``sandbox-unavailable`` warning, or None if there is nothing new
        to warn about. Idempotent — a repeated identical reason is only surfaced once per process."""
        reason = self._pending_warning
        self._pending_warning = None
        if reason is None or reason in self._warned:
            return None
        self._warned.add(reason)
        return reason


def default_backend() -> SandboxBackend:
    """The OS-default backend for the ``auto`` selection (the only selection this landing ships).

    NOTE (deferred, docs/CLAUDE-SANDBOX-PLAN.md §5.4 / Decision 2): a user-facing
    ``sandbox.backend = auto | native | srt`` config selector and an ``SrtBackend`` (which delegates
    to ``@anthropic-ai/sandbox-runtime`` and additionally unlocks network egress control for a
    ``strict`` posture — accepting a Node.js dependency) slot in HERE behind the same
    :class:`SandboxBackend` seam. For now ``auto`` hardcodes the cairn-owned :class:`NativeBackend`;
    a config selector is a later one-liner that returns a different backend from this function.
    """
    return NativeBackend()


def build_wrapper(posture: str) -> SandboxWrapper:
    """Build a :class:`SandboxWrapper` for ``posture``. ``off`` gets no backend (pure passthrough,
    zero cost); an active posture gets the OS-default backend."""
    backend = default_backend() if posture in _ACTIVE_POSTURES else None
    return SandboxWrapper(posture, backend)
