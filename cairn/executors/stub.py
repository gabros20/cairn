"""The ``stub`` executor — the L1 test executor (TESTING.md §5).

``stub`` implements the normal executor protocol, so it is selectable like any other
(``cairn run … --executor stub``, or the pipeline test suite's stub matrix). Instead of
invoking a model it *replays reality*: it copies a canned artifact tree from
``tests/stubs/<pipeline>/<step>[.c<cycle>]/`` into the run dir and returns a canned STEP
block. Everything downstream — validators, gates, loops, resume — is production code, so
one offline suite exercises the whole pipeline for zero tokens.

Identity & location, all derived from the invocation (walk.py never sets a pipeline env):

* **step**     — ``inv.env["CAIRN_STEP"]`` (walk's ``_build_env`` sets this).
* **pipeline** — read from ``<run_dir>/run.json`` (``inv.cwd`` is the run dir; the manifest
  records ``pipeline``). walk sets ``CAIRN_STEP`` but *not* ``CAIRN_PIPELINE``.
* **cycle**    — parsed from the ``.cK`` suffix of ``inv.log_path``'s stem (the naming
  contract is ``logs/<id>[.rN][.cK].log`` — walk's ``_log_path``); ``None`` outside a loop.
* **stubs_root** — the ctor arg if given, else ``$CAIRN_STUBS_ROOT``, else
  ``<CAIRN_WORKSPACE>/tests/stubs`` (the executor learns the workspace only at invoke time,
  from ``inv.env["CAIRN_WORKSPACE"]``).

The replay: a cycle-suffixed dir (``<step>.c<cycle>/``) is preferred when it exists, else the
bare ``<step>/`` dir. Its entire tree is copied into ``inv.cwd`` (overlaying), except a
sidecar ``_step.json`` — which, when present, *is* the returned STEP verbatim; otherwise the
STEP is synthesized as ``done`` over the copied files. A missing stub dir returns
``Result(step=None, exit_code=1)`` so the walker's artifact gate fails loudly with the
artifact-missing reasons (never a silent green).

Stdlib + pinned kernel modules only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from cairn.executors.base import Capabilities, Finding, Invocation, Result

_SIDECAR = "_step.json"
# Run control files/dirs a stub tree must never overwrite — defense-in-depth: `record` never
# emits them, but a hand-authored stub could smuggle a poisoned run.json / trail / gate.
_PROTECTED = frozenset({"run.json", ".cairn", "logs", "trail.jsonl", "gates", ".cairn.lock"})
_SUFFIX_SEG = re.compile(r"[rc]\d+")


class StubExecutor:
    name = "stub"
    capabilities = Capabilities(
        blocking_hooks=False,
        output_schema=False,
        session_capture=None,
        installs_hooks=False,  # stub never installs a hook
    )

    def __init__(self, config=None, *, stubs_root: Path | None = None) -> None:
        # Accepts an (ignored) ExecutorConfig so the walker can build every executor
        # uniformly; ``stubs_root`` pins the fixtures dir (else it is resolved per-invoke).
        self._config = config
        self._stubs_root = Path(stubs_root) if stubs_root is not None else None

    def doctor(self) -> list[Finding]:
        where = str(self._stubs_root) if self._stubs_root else "<workspace>/tests/stubs (per-invoke)"
        return [Finding("info", f"stub executor healthy; stubs_root = {where}")]

    def resolve_model(self, tier: str, effort: str) -> tuple[str, str | None]:
        return ("stub", None)

    def invoke(self, inv: Invocation) -> Result:
        step = inv.env.get("CAIRN_STEP", "")
        pipeline = self._pipeline(inv)
        cycle = self._cycle_from_log(inv.log_path, step)
        root = self._resolve_stubs_root(inv)

        stub_dir = self._locate(root, pipeline, step, cycle) if root is not None else None
        if stub_dir is None:
            # No canned artifacts to replay → let the artifact gate fail loudly with the
            # "artifact missing" reasons, rather than reporting a false green.
            return Result(step=None, exit_code=1, duration_s=0.0)

        copied = self._overlay(stub_dir, Path(inv.cwd))
        step_block = self._step_block(stub_dir, step, copied)
        return Result(step=step_block, exit_code=0, duration_s=0.0)

    def install_guards(self, guards, ws, run_dir) -> None:
        return None

    def render_workspace(self, ws) -> None:
        return None

    # -- identity / location derivation ------------------------------------- #

    @staticmethod
    def _pipeline(inv: Invocation) -> str | None:
        """Read the pipeline name from the run dir's ``run.json`` (inv.cwd is the run dir)."""
        run_json = Path(inv.cwd) / "run.json"
        try:
            return json.loads(run_json.read_text(encoding="utf-8")).get("pipeline")
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _cycle_from_log(log_path: Path, step: str) -> int | None:
        """Parse the ``.cK`` loop-cycle suffix from ``logs/<id>[.rN][.cK].log``.

        A dotted step id (``a.c1``) could look like it carries a cycle suffix, so the parse is
        anchored to the known ``step`` id: the stem must be ``<step>`` (→ no cycle) or
        ``<step>.<suffix>`` where every ``.`` segment of ``<suffix>`` is an ``rN``/``cN`` token;
        anything else yields ``None`` rather than a phantom cycle.
        """
        stem = Path(log_path).stem
        if not step or stem == step:
            return None
        prefix = f"{step}."
        if not stem.startswith(prefix):
            return None
        segments = stem[len(prefix):].split(".")
        if not all(_SUFFIX_SEG.fullmatch(seg) for seg in segments):
            return None
        for seg in segments:
            if seg.startswith("c"):
                return int(seg[1:])
        return None

    def _resolve_stubs_root(self, inv: Invocation) -> Path | None:
        if self._stubs_root is not None:
            return self._stubs_root
        env_root = inv.env.get("CAIRN_STUBS_ROOT") or os.environ.get("CAIRN_STUBS_ROOT")
        if env_root:
            return Path(env_root)
        workspace = inv.env.get("CAIRN_WORKSPACE")
        if not workspace:
            return None  # can't locate a stubs dir → treated as a missing stub (exit 1)
        return Path(workspace) / "tests" / "stubs"

    @staticmethod
    def _locate(root: Path, pipeline: str | None, step: str, cycle: int | None) -> Path | None:
        """The stub dir for this step: prefer the cycle-suffixed dir, else the bare dir."""
        if not pipeline or not step:
            return None
        base = root / pipeline
        if cycle is not None:
            cycled = base / f"{step}.c{cycle}"
            if cycled.is_dir():
                return cycled
        bare = base / step
        return bare if bare.is_dir() else None

    # -- replay ------------------------------------------------------------- #

    @staticmethod
    def _overlay(stub_dir: Path, run_dir: Path) -> list[str]:
        """Copy the stub tree into ``run_dir`` (overlaying), skipping the ``_step.json``
        sidecar. Returns the run-dir-relative paths of the copied files, sorted."""
        copied: list[str] = []
        for src in sorted(stub_dir.rglob("*")):
            rel = src.relative_to(stub_dir)
            if rel.parts[0] in _PROTECTED:
                continue  # never let a stub tree clobber the run's control files/dirs
            if src.is_dir():
                (run_dir / rel).mkdir(parents=True, exist_ok=True)
                continue
            if rel == Path(_SIDECAR):
                continue  # the canned STEP sidecar is metadata, never an artifact
            dest = run_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied.append(rel.as_posix())
        return sorted(copied)

    @staticmethod
    def _step_block(stub_dir: Path, step: str, copied: list[str]) -> dict:
        """The canned STEP: the ``_step.json`` sidecar verbatim, else a synthesized ``done``."""
        sidecar = stub_dir / _SIDECAR
        if sidecar.is_file():
            try:
                obj = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                obj = None
            if isinstance(obj, dict):
                return obj
        return {
            "status": "done",
            "summary": f"stub replay of {step}",
            "artifacts": copied,
        }
