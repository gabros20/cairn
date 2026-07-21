"""The artifact store — naming, globbing, and the schema+validator gate.

Artifacts are the edges of the pipeline graph and the *done* predicate of every step
(CONCEPTS §4). This module owns three jobs, all pure and derived from disk:

  * **parse** the pipeline's ``artifacts:`` mapping into typed declarations, verifying
    that every referenced schema/validator file exists (API §2.2);
  * **resolve** a rendered path (plain or ``*``-glob) to concrete paths *inside* a run
    dir, never escaping it (API §2.8);
  * **validate** an artifact against its JSON Schema and/or its external validator — the
    only arbiter of done-ness (ARCHITECTURE §3.1, §4; API §4).

Path templates ({cycle}/{params.x}) are rendered by the planner, not here: this module
is handed already-resolved path strings. Stdlib + jsonschema only; the validator
subprocess is the single sanctioned side effect.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from cairn.kernel.errors import ConfigError

# One validator run's wall-clock ceiling (API §4). A parameter on every entry point so
# tests can inject a tiny timeout against a sleeping fixture.
DEFAULT_VALIDATOR_TIMEOUT_S = 60


@dataclass(frozen=True)
class ArtifactDecl:
    """One artifact's declared contract (API §2.2).

    ``path`` is the raw template string exactly as authored — it may still contain
    ``{cycle}``/``{params.x}``; rendering is the planner's job, this module stores it
    verbatim. ``schema``/``validator`` are resolved to files under the workspace. At
    least one of the two is required (enforced at parse time).
    """

    name: str
    path: str
    schema: Path | None = None
    validator: Path | None = None
    describe: str | None = None


@dataclass(frozen=True)
class ResolvedArtifact:
    """A declaration resolved against a run dir to concrete absolute path(s).

    Plain paths resolve to exactly one entry (whether or not it exists yet); glob
    patterns resolve to the sorted set of matches (empty when nothing matches).
    ``rendered_path`` keeps the run-dir-relative path string the planner rendered (the
    literal path for plain artifacts, the pattern for globs) — it is the third argv
    element passed to external validators (API §4).
    """

    name: str
    paths: list[Path]
    rendered_path: str


@dataclass(frozen=True)
class ValidationResult:
    """The verdict for one artifact. ``reasons`` are machine-readable lines fed verbatim
    into the trail, halt message, and retry envelope (API §4)."""

    ok: bool
    reasons: list[str]


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _is_glob(path: str) -> bool:
    return "*" in path


def parse_artifacts(raw: dict[str, Any], workspace_dir: Path) -> dict[str, ArtifactDecl]:
    """Parse the pipeline's ``artifacts:`` mapping into declarations keyed by name.

    ``raw`` is the already-yaml-loaded ``artifacts:`` dict (a mapping of name → spec).
    Every referenced schema/validator path is resolved under ``workspace_dir`` and must
    exist; a missing file or a declaration with neither schema nor validator raises
    ConfigError naming the artifact and the offending path.
    """
    workspace_dir = Path(workspace_dir)
    decls: dict[str, ArtifactDecl] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"artifact {name!r} must be a mapping")

        path = spec.get("path")
        if not isinstance(path, str) or not path:
            raise ConfigError(f"artifact {name!r} requires a non-empty string 'path'")

        schema = _resolve_ref(name, "schema", spec.get("schema"), workspace_dir)
        validator = _resolve_ref(name, "validator", spec.get("validator"), workspace_dir)
        if schema is None and validator is None:
            raise ConfigError(
                f"artifact {name!r} needs at least one of 'schema' or 'validator'"
            )

        describe = spec.get("describe")
        decls[name] = ArtifactDecl(
            name=name,
            path=path,
            schema=schema,
            validator=validator,
            describe=describe,
        )
    return decls


def _resolve_ref(
    name: str, kind: str, value: Any, workspace_dir: Path
) -> Path | None:
    """Resolve a schema/validator reference to an existing file under the workspace."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"artifact {name!r} {kind} must be a workspace-relative path")
    resolved = workspace_dir / value
    if not resolved.is_file():
        raise ConfigError(
            f"artifact {name!r} references missing {kind} file: {value}"
        )
    return resolved


# --------------------------------------------------------------------------- #
# Resolution & existence
# --------------------------------------------------------------------------- #


def _assert_contained(
    decl_name: str, rendered_path: str, candidate: Path, real_run_dir: Path
) -> None:
    """Resolve ``candidate`` (following symlinks) and require it to stay under ``real_run_dir``
    (the caller's already-resolved run dir — resolved ONCE per ``resolve_path`` call, not
    once per glob match).

    The string checks in ``resolve_path`` catch an absolute/``..`` rendered path before any
    disk access, but they cannot catch a SYMLINK: a run-local path like ``run/report.json``
    can pass those checks while pointing (via a symlink somewhere in its chain) at
    ``/etc/passwd`` or any other host file — a valid external file would then falsely satisfy
    the artifact's schema/validator, and reads would escape the run dir (codex-F11).

    ``resolve()`` is strict=False: a not-yet-existing artifact leaf (the normal case — the
    step will write it) still resolves fine as long as its PARENT chain contains no
    escaping symlink, so this never rejects the common "artifact doesn't exist yet" path.

    Known residual (TOCTOU, disclosed not closed): ``ResolvedArtifact.paths`` stores the
    UNRESOLVED candidate (``run_dir / rendered_path``, or the raw glob match), not the
    resolved path checked here — downstream readers (``validate()``, ``walk.py``'s artifact
    JSON reads) re-touch the filesystem through that unresolved path. In the narrow
    same-process window between this check and that later read, a symlink swapped in by the
    sandboxed agent (who holds write access to the run dir) could still redirect the read.
    Storing the resolved path instead would close most of that window, but would also change
    what downstream code sees: ``validate()._rel`` computes paths relative to the (unresolved)
    ``run_dir`` it's separately handed, and a resolved leaf under a differently-resolved
    ``run_dir`` (e.g. a symlinked run root) could fail that ``relative_to`` and fall back to
    printing an absolute path in artifact-missing/schema-violation reasons — a downstream
    contract change this cleanup should not force. Accepted as a documented residual; see
    SECURITY.md §1.4.
    """
    resolved = candidate.resolve()
    if resolved != real_run_dir and real_run_dir not in resolved.parents:
        raise ConfigError(
            f"artifact {decl_name!r} path escapes the run dir via symlink: {rendered_path}"
        )


def resolve_path(decl: ArtifactDecl, rendered_path: str, run_dir: Path) -> ResolvedArtifact:
    """Resolve an already-rendered path to concrete path(s) inside ``run_dir``.

    Plain paths → ``[run_dir / rendered_path]``. Glob patterns (containing ``*``) →
    the sorted set of matches under the run dir (empty when none match). The path may
    never escape the run dir: an absolute path or one containing ``..`` is a ConfigError
    (checked first, fail-fast before touching disk), nor may it escape via a SYMLINK
    (checked by resolving each concrete candidate — see ``_assert_contained``).
    """
    run_dir = Path(run_dir)
    candidate = Path(rendered_path)
    if candidate.is_absolute():
        raise ConfigError(
            f"artifact {decl.name!r} path escapes the run dir (absolute): {rendered_path}"
        )
    if ".." in candidate.parts:
        raise ConfigError(
            f"artifact {decl.name!r} path escapes the run dir (..): {rendered_path}"
        )

    real_run_dir = run_dir.resolve()  # resolved ONCE — every candidate is checked against it
    if _is_glob(rendered_path):
        # A glob names a set of *files*: drop directory matches, and expand a trailing
        # bare ``**`` to ``**/*`` (pathlib's bare ``**`` matches directories only), so the
        # kernel and the scaffold's nonempty.py agree on what ``blueprints/**`` means.
        pattern = f"{rendered_path}/*" if rendered_path.endswith("**") else rendered_path
        paths = sorted(p for p in run_dir.glob(pattern) if p.is_file())
        for p in paths:
            _assert_contained(decl.name, rendered_path, p, real_run_dir)
    else:
        paths = [run_dir / rendered_path]
        _assert_contained(decl.name, rendered_path, paths[0], real_run_dir)
    return ResolvedArtifact(name=decl.name, paths=paths, rendered_path=rendered_path)


def exists(resolved: ResolvedArtifact) -> bool:
    """Whether the artifact is present: a plain path's file exists, or a glob has ≥1 match.

    Glob resolution already filters to existing matches, so a non-empty ``paths`` whose
    entries all exist covers both cases uniformly.
    """
    return bool(resolved.paths) and all(p.exists() for p in resolved.paths)


# --------------------------------------------------------------------------- #
# Validation — the arbiter of done-ness
# --------------------------------------------------------------------------- #


def validate(
    resolved: ResolvedArtifact,
    decl: ArtifactDecl,
    run_dir: Path,
    workspace_dir: Path,
    timeout_s: int = DEFAULT_VALIDATOR_TIMEOUT_S,
) -> ValidationResult:
    """Validate one resolved artifact against its schema and/or validator.

    A missing file short-circuits to a failure *without* running the validator. Otherwise
    a JSON Schema (if declared) is checked against every matched ``.json`` file, and the
    external validator (if declared) is run; both contribute reasons. ``ok`` iff no reasons.
    """
    run_dir = Path(run_dir)
    if not exists(resolved):
        return ValidationResult(
            ok=False, reasons=[f"artifact missing: {resolved.rendered_path}"]
        )

    reasons: list[str] = []
    if decl.schema is not None:
        reasons.extend(_schema_reasons(resolved, decl.schema, run_dir))
    if decl.validator is not None:
        reasons.extend(_validator_reasons(resolved, decl, run_dir, workspace_dir, timeout_s))
    return ValidationResult(ok=not reasons, reasons=reasons)


def _rel(path: Path, run_dir: Path) -> str:
    try:
        return str(path.relative_to(run_dir))
    except ValueError:
        return str(path)


def _schema_reasons(resolved: ResolvedArtifact, schema_file: Path, run_dir: Path) -> list[str]:
    """JSON-Schema every matched ``.json`` file: parse errors and violations become reasons.

    A schema that matched *no* JSON file (a plain path that is a directory, or a glob
    hitting only non-JSON files) is a failure, not a silent pass — otherwise declaring a
    schema over the wrong path would validate nothing yet report done.
    """
    schema = json.loads(schema_file.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    reasons: list[str] = []
    checked = 0
    for path in resolved.paths:
        if path.suffix != ".json" or not path.is_file():
            continue
        checked += 1
        rel = _rel(path, run_dir)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            reasons.append(f"{rel}: invalid JSON: {exc}")
            continue
        for error in sorted(validator.iter_errors(document), key=lambda e: e.path):
            location = "/".join(str(p) for p in error.path)
            where = f" at {location}" if location else ""
            reasons.append(f"{rel}{where}: {error.message}")
    if checked == 0:
        reasons.append(f"schema declared but no JSON matched: {resolved.rendered_path}")
    return reasons


def _validator_reasons(
    resolved: ResolvedArtifact,
    decl: ArtifactDecl,
    run_dir: Path,
    workspace_dir: Path,
    timeout_s: int,
) -> list[str]:
    """Run the external validator (API §4): exit 0 pass; exit 1 → stdout reasons; else fail.

    argv is ``[run_dir, artifact_name, artifact_path]`` — the third element is the
    rendered run-dir-relative path (the pattern for glob artifacts).
    """
    validator = decl.validator
    assert validator is not None
    argv_tail = [str(run_dir), decl.name, resolved.rendered_path]
    if validator.suffix == ".py":
        cmd = [sys.executable, str(validator), *argv_tail]
    else:
        cmd = [str(validator), *argv_tail]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workspace_dir),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return [f"validator {validator.name} timed out after {timeout_s}s"]
    except OSError as exc:
        return [f"validator {validator.name} could not be run: {exc}"]

    if proc.returncode == 0:
        return []
    if proc.returncode == 1:
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        return lines if lines else ["validator failed without reasons"]
    stderr_lines = [ln for ln in proc.stderr.splitlines() if ln.strip()]
    detail = f": {stderr_lines[-1]}" if stderr_lines else ""
    return [f"validator {validator.name} exited with code {proc.returncode}{detail}"]


# --------------------------------------------------------------------------- #
# Aggregates — the step-done predicate and loop-cycle counting
# --------------------------------------------------------------------------- #


def done(
    resolved_list: list[ResolvedArtifact],
    decls: dict[str, ArtifactDecl],
    run_dir: Path,
    workspace_dir: Path,
    timeout_s: int = DEFAULT_VALIDATOR_TIMEOUT_S,
) -> tuple[bool, dict[str, ValidationResult]]:
    """The step-done predicate over a step's ``produces`` (ARCHITECTURE §3.1).

    ``done(step) ⇔ ∀ a ∈ produces: exists(a) ∧ validate(a) = pass``. Returns the overall
    verdict plus the per-artifact result, so a halt can report exactly which one failed.
    """
    results = {
        r.name: validate(r, decls[r.name], run_dir, workspace_dir, timeout_s)
        for r in resolved_list
    }
    return all(res.ok for res in results.values()), results


def count_cycles(decl: ArtifactDecl, run_dir: Path, render: Callable[[int], str]) -> int:
    """Count the consecutive existing cycle artifacts, from cycle 1 (ARCHITECTURE §3.4).

    ``render(n)`` renders ``decl.path`` with ``{cycle}=n``. Returns the highest N for which
    cycles 1..N all exist — a gap stops the count (r1,r2,r4 → 2). Existence only; validity
    is the walker's concern.
    """
    n = 0
    while exists(resolve_path(decl, render(n + 1), run_dir)):
        n += 1
    return n
