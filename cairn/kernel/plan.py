"""The planner — ``(workspace, pipeline, params) → Plan | ConfigError`` (ARCHITECTURE §2).

A pure function. It never writes a file, never spawns a process: it loads the pipeline
and everything it references, resolves params into dims, evaluates the conditionals it
can settle now, verifies the dataflow graph and every reference, and emits a typed
:class:`Plan` the walker executes. Its whole value is failing *here*, with a file-named
diagnostic, so the class of "phase 4 crashed because phase 2's output name was typo'd"
dies before a single token is spent.

The six steps (ARCHITECTURE §2), in order:

1. **LOAD**   — parse ``pipelines/<name>.yaml`` + ``cairn.toml``; schema-check shapes.
2. **RESOLVE**— coerce/validate params (string|enum|int), derive dims via the preset table.
3. **EXPAND** — settle every ``when:``/``unless:`` that params/dims decide *now*
   (short-circuit aware, so ``params.x=='on' && gates…`` drops cleanly when ``x`` is off);
   the rest become runtime predicates on the node. Loop ``until:`` is always runtime.
4. **DATAFLOW**— walk nodes in order, tracking produced names (artifacts + gate names);
   every ``needs`` must already be produced, duplicate produce is an error (except the
   sanctioned loop-body re-production), ``needs_optional`` must be *declared* but may be
   produced by a conditionally-dropped node.
5. **REFERENCES** — agent files, skills, secrets, allowlist fragments, template
   placeholders and expressions all resolve; executor resolution is lazy (only demanded
   when an ``agent:`` step is in range).
6. **EMIT**   — a :class:`Plan` of frozen nodes, sliced to ``--from``/``--to``.

Stdlib + pyyaml + jsonschema only. Node dataclasses live here for now; the walker and
compose import them from this module.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from cairn.kernel.artifacts import ArtifactDecl, parse_artifacts
from cairn.kernel.config import Config, check_requires, load_config
from cairn.kernel.errors import ConfigError
from cairn.kernel.expr import EvalError, Expr, ExprError
from cairn.kernel.expr import parse as parse_expr
from cairn.kernel.template import (
    TemplateContext,
    TemplateError,
    render,
    scan,
)
from cairn.kernel.types import EFFORTS, TIERS, Finding

# --------------------------------------------------------------------------- #
# The node model — frozen dataclasses the walker and compose build on.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AgentSpec:
    """An ``agents/<name>.yaml`` resolved for one step. ``tier`` is *after* escalation."""

    name: str
    tier: str
    effort: str | None
    skills: tuple[str, ...]
    tools_allow: tuple[str, ...]
    bash_fragment: str | None
    env: tuple[str, ...]
    network: bool
    returns: str


@dataclass(frozen=True)
class StepNode:
    """A concrete executable step. ``kind`` picks the payload: an ``agent:`` invocation,
    a deterministic ``run:`` command, or a ``manual:`` operator instruction (both stored in
    ``command``). ``executor``/``tier``/``effort`` are the resolved routing for agent steps
    (None for run/manual — those go to the shell executor)."""

    id: str
    kind: str  # "agent" | "run" | "manual"
    agent: AgentSpec | None
    command: str | None
    args: dict[str, Any]
    needs: tuple[str, ...]
    needs_optional: tuple[str, ...]
    produces: tuple[str, ...]
    when_runtime: Expr | None
    timeout_s: int
    retry: tuple[int, bool]  # (attempts, feedback)
    skippable: bool
    executor: str | None
    tier: str | None
    effort: str | None
    env: tuple[str, ...]
    network: bool


@dataclass(frozen=True)
class GateNode:
    """A human decision point. ``options`` is an ordered tuple of ``(key, description)``;
    ``default`` is the headless resolution (one of the option keys). The gate's ``name`` is
    itself a consumable artifact once resolved (``gates/<name>.json``)."""

    name: str
    reads: tuple[str, ...]
    ask: str
    options: tuple[tuple[str, str], ...]
    default: str
    when_runtime: Expr | None


@dataclass(frozen=True)
class ParallelNode:
    """A concurrent group. Children run as independent processes; their ``produces`` must be
    disjoint (verified at plan time)."""

    name: str
    on_fail: str  # "wait_all" | "fast"
    steps: tuple[StepNode, ...]
    when_runtime: Expr | None


@dataclass(frozen=True)
class LoopNode:
    """A bounded review⇄revise loop. ``until`` is always a runtime predicate (may be None
    to mean "run to the cap"); the cap depends on headless-ness, so both are recorded."""

    name: str
    min: int
    max_interactive: int
    max_headless: int
    until: Expr | None
    on_cap: str  # "halt" | "continue"
    body: tuple["Node", ...]
    when_runtime: Expr | None


Node = StepNode | GateNode | ParallelNode | LoopNode


@dataclass(frozen=True)
class GuardDecl:
    """One ``guards:`` entry. ``when`` is a runtime predicate when it references runtime
    roots, else None (a guard that params/dims decide to be inactive is dropped entirely)."""

    name: str
    match_tool: str
    match_command: str
    check: Path
    enforce: tuple[str, ...]
    on_error: str  # "allow" | "deny"
    when: Expr | None


@dataclass(frozen=True)
class PlanSkip:
    """A node dropped at plan time because params/dims settled its ``when:``/``unless:``."""

    node: str
    kind: str  # "step" | "gate" | "parallel" | "loop"
    reason: str


@dataclass(frozen=True)
class ToolRequirement:
    """A ``[tools.X]`` entry whose ``needed_by`` scope names an in-range step (or this pipeline).

    Computed at plan time — offline, so ``check`` is NOT run here; it is carried whole so
    ``cairn run`` can probe it before minting anything (docs/TOOLING-AND-GROWTH.md §2). ``targets``
    is the sorted in-range step ids the scope names, plus the pipeline name when pipeline-scoped —
    the "needed by" list a refusal prints. Unscoped tools never appear here (doctor's concern)."""

    tool: str
    check: str
    install: str | None
    targets: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    """The emitted, executable plan. ``nodes`` is the ``--from``/``--to`` slice; dataflow and
    references are verified over the whole pipeline before slicing. ``resolved_models`` maps a
    step id → ``(executor, model, effort)`` for every agent step *in range*. ``tool_requirements``
    is the range-scoped subset of ``[tools]`` (see :class:`ToolRequirement`) — the hard-stop set
    ``cairn run`` verifies before it mints or walks anything."""

    pipeline: str
    version: int
    params: dict[str, Any]
    dims: dict[str, Any]
    run_id_template: str
    nodes: tuple[Node, ...]
    artifacts: dict[str, ArtifactDecl]
    guards: tuple[GuardDecl, ...]
    warnings: list[Finding]
    executor_default: str | None
    resolved_models: dict[str, tuple[str, str, str | None]]
    skipped: tuple[PlanSkip, ...] = ()
    tool_requirements: tuple[ToolRequirement, ...] = ()


# --------------------------------------------------------------------------- #
# Error helper + plan-time expression evaluation.
# --------------------------------------------------------------------------- #


def _err(message: str, file: str | None = None) -> "Any":
    raise ConfigError(message, findings=[Finding("error", message)], file=file)


class _Deferred(EvalError):
    """Signals that a path root is only knowable at runtime (artifacts/gates/run/cycle).

    Raised by the plan-time resolver; thanks to ``&&``/``||`` short-circuit it is never
    raised for the *dead* side of a settled boolean, so ``params.x=='on' && gates.y…``
    settles to False (drop) whenever ``x`` is off, without demanding the gate."""


_DEFER = object()


def _make_plan_resolver(params: dict[str, Any], dims: dict[str, Any]) -> Callable[[str, tuple[str, ...]], Any]:
    def resolve(root: str, parts: tuple[str, ...]) -> Any:
        if root in ("params", "dims"):
            table = params if root == "params" else dims
            if not parts:
                raise EvalError(f"{root} needs a name (e.g. {root}.mode)")
            key = parts[0]
            if key not in table:
                raise EvalError(f"unknown path: {root}.{key}")
            return table[key]
        # artifacts / gates / run / cycle — only knowable at runtime.
        raise _Deferred(f"{root} is a runtime root")

    return resolve


# --------------------------------------------------------------------------- #
# Planning context (mutable scratch shared across the parse/verify passes).
# --------------------------------------------------------------------------- #


@dataclass
class _Ctx:
    workspace_dir: Path
    config: Config
    file: str
    resolver: Callable[[str, tuple[str, ...]], Any]
    params: dict[str, Any]
    dims: dict[str, Any]
    artifact_names: set[str]
    gate_names: set[str]
    warnings: list[Finding]
    _allowlist: set[str] | None = None
    _allowlist_ok: bool = False


# --------------------------------------------------------------------------- #
# Step 2 — params & dims.
# --------------------------------------------------------------------------- #


def _resolve_params(specs: Any, provided: dict[str, str], file: str) -> dict[str, Any]:
    specs = specs or {}
    if not isinstance(specs, dict):
        _err("params: must be a mapping", file)
    resolved: dict[str, Any] = {}
    for name, spec in specs.items():
        spec = spec or {}
        typ = spec.get("type", "string")
        if typ not in ("string", "enum", "int"):
            _err(f"param {name!r}: unknown type {typ!r} (string|enum|int)", file)
        values = spec.get("values")
        if typ == "enum" and not values:
            _err(f"param {name!r}: an enum param needs a non-empty 'values' list", file)

        if name in provided:
            resolved[name] = _coerce_param(name, typ, values, provided[name], file, is_default=False)
        elif "default" in spec:
            resolved[name] = _coerce_param(name, typ, values, spec["default"], file, is_default=True)
        elif spec.get("required"):
            _err(f"param {name!r} is required — pass --param {name}=…", file)
        else:
            resolved[name] = None

    for key in provided:
        if key not in specs:
            _err(f"unknown param {key!r} (not declared by the pipeline)", file)
    return resolved


def _coerce_param(name: str, typ: str, values: Any, raw: Any, file: str, *, is_default: bool) -> Any:
    origin = "default" if is_default else "value"
    if typ == "int":
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            _err(f"param {name!r} {origin} must be an integer, got {raw!r}", file)
        try:
            return int(raw)
        except (TypeError, ValueError):
            _err(f"param {name!r} {origin} must be an integer, got {raw!r}", file)
    if typ == "enum":
        if raw not in values:
            _err(f"param {name!r} {origin} {raw!r} must be one of {list(values)}", file)
        return raw
    return str(raw)


def _derive_dims(dims_raw: Any, params: dict[str, Any], file: str) -> dict[str, Any]:
    dims_raw = dims_raw or {}
    if not dims_raw:
        return {}
    if not isinstance(dims_raw, dict):
        _err("dims: must be a mapping", file)
    frm = dims_raw.get("from")
    presets = dims_raw.get("presets", {}) or {}
    if not frm:
        _err("dims: needs 'from' naming the driving param", file)
    if frm not in params:
        _err(f"dims.from names {frm!r}, which is not a declared param", file)
    key = params[frm]
    if key not in presets:
        _err(f"dims: no preset for {frm}={key!r} (presets: {list(presets)})", file)
    preset = presets[key]
    if not isinstance(preset, dict):
        _err(f"dims.presets.{key} must be a mapping", file)
    return dict(preset)


# --------------------------------------------------------------------------- #
# Step 3 — conditional expansion.
# --------------------------------------------------------------------------- #


def _parse_cond(src: str, label: str, ctx: _Ctx) -> Expr:
    try:
        return parse_expr(src)
    except ExprError as exc:
        _err(f"{label}: cannot parse expression {src!r}: {exc}", ctx.file)


def _cond_value(expr: Expr, label: str, ctx: _Ctx) -> Any:
    try:
        return expr.evaluate(ctx.resolver)
    except _Deferred:
        return _DEFER
    except EvalError as exc:
        _err(f"{label}: {exc}", ctx.file)


def _static_check_paths(expr: Expr, label: str, ctx: _Ctx) -> None:
    """Leaf-check every ``params.``/``dims.`` path syntactically — independent of evaluation.

    ``Expr.paths()`` sees both operands of every comparison and *both sides* of a
    short-circuited ``&&``/``||``, so a misspelling on the lazy branch
    (``gates.g.choice=='yes' && params.NOPE=='z'``) can no longer hide until runtime.
    Runtime roots (artifacts/gates/run/cycle) are not checked — they are unknowable now.

    Known limitation: a node dropped by its own plan-time ``when:``/``unless:`` is never
    parsed, so a params/dims typo *inside that dropped subtree* is not checked here — such a
    typo surfaces only in the modes where the node is live (and is caught then).
    """
    for root, parts in expr.paths():
        if root not in ("params", "dims"):
            continue
        table = ctx.params if root == "params" else ctx.dims
        if not parts:
            _err(f"{label}: bare {{{root}}} is not a value — use {root}.<name>", ctx.file)
        if parts[0] not in table:
            _err(f"{label}: unknown path {root}.{parts[0]}", ctx.file)


def _expand_condition(raw: dict, label: str, ctx: _Ctx) -> tuple[str, Expr | None, str | None]:
    """Settle ``when:``/``unless:`` as far as params/dims allow.

    Returns ``(decision, when_runtime, reason)`` — ``decision`` is ``"keep"`` or ``"drop"``.
    A predicate the resolver can settle now decides drop/keep here; one that touches a
    runtime root is combined into a single runtime Expr on the kept node.
    """
    # Parse + statically leaf-check every condition FIRST, so a params/dims typo is caught
    # even when short-circuit would settle the node (drop) before that branch is evaluated.
    conds: list[tuple[str, str, Expr]] = []
    for key in ("when", "unless"):
        src = raw.get(key)
        if src is None:
            continue
        if not isinstance(src, str):
            _err(f"{label}: {key}: must be an expression string", ctx.file)
        expr = _parse_cond(src, f"{label} {key}", ctx)
        _static_check_paths(expr, f"{label} {key}", ctx)
        conds.append((key, src, expr))

    runtime: list[tuple[str, str]] = []
    for key, src, expr in conds:
        value = _cond_value(expr, f"{label} {key}", ctx)
        if value is _DEFER:
            runtime.append((key, src))
            continue
        truth = bool(value)
        if key == "when" and not truth:
            return "drop", None, f"when: {src} is false"
        if key == "unless" and truth:
            return "drop", None, f"unless: {src} is true"

    if not runtime:
        return "keep", None, None
    terms = [f"({src})" if key == "when" else f"!({src})" for key, src in runtime]
    combined = _parse_cond(" && ".join(terms), f"{label} runtime condition", ctx)
    return "keep", combined, None


# --------------------------------------------------------------------------- #
# Step 5 (part) — agents, secrets, skills, allowlist.
# --------------------------------------------------------------------------- #


def _allowlist_fragments(ctx: _Ctx) -> set[str]:
    if ctx._allowlist is None:
        path = ctx.workspace_dir / "allowlist.yaml"
        ctx._allowlist_ok = path.is_file()
        if ctx._allowlist_ok:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            ctx._allowlist = set(doc.keys()) if isinstance(doc, dict) else set()
        else:
            ctx._allowlist = set()
    return ctx._allowlist


def _check_allowlist_fragment(bash: str, agent_name: str, ctx: _Ctx, file: str) -> None:
    file_part, sep, frag = bash.partition("#")
    if not sep or not frag:
        _err(f"agent {agent_name!r}: bash must be 'allowlist.yaml#fragment', got {bash!r}", file)
    if not (ctx.workspace_dir / file_part).is_file():
        _err(f"agent {agent_name!r}: bash allowlist file {file_part!r} not found", file)
    if frag not in _allowlist_fragments(ctx):
        _err(f"agent {agent_name!r}: allowlist fragment #{frag} not found in {file_part}", file)


def _apply_escalate(
    doc: dict, name: str, base_tier: str, base_effort: str | None, ctx: _Ctx, file: str
) -> tuple[str, str | None]:
    """Resolve escalation to the effective ``(tier, effort)``. When the escalation FIRES it
    bumps the tier and — if ``escalate.effort`` is given — overrides the effort (escalate.effort
    beats the agent's own effort, which beats the tier's; see :func:`_resolve_exec`). When it does
    not fire, or ``escalate`` is absent, the base ``(tier, effort)`` pass through unchanged.
    ``escalate.tier`` and ``escalate.effort`` are validated unconditionally, so a typo is caught
    even in a param set where the escalation is dormant."""
    esc = doc.get("escalate")
    if not esc:
        return base_tier, base_effort
    if not isinstance(esc, dict):
        _err(f"agent {name!r}: escalate must be a mapping", file)
    esc_tier = esc.get("tier")
    if esc_tier not in TIERS:
        _err(f"agent {name!r}: escalate.tier {esc_tier!r} invalid (valid: {list(TIERS)})", file)
    esc_effort = esc.get("effort")
    if esc_effort is not None and esc_effort not in EFFORTS:
        _err(f"agent {name!r}: escalate.effort {esc_effort!r} invalid (valid: {list(EFFORTS)})", file)
    when_src = esc.get("when")
    if not when_src:
        _err(f"agent {name!r}: escalate needs a 'when' expression", file)
    expr = _parse_cond(when_src, f"agent {name!r} escalate.when", ctx)
    if not expr.roots() <= {"params", "dims"}:
        _err(f"agent {name!r}: escalate.when may only reference params/dims", file)
    try:
        fires = bool(expr.evaluate(ctx.resolver))
    except EvalError as exc:
        _err(f"agent {name!r}: escalate.when: {exc}", file)
    if not fires:
        return base_tier, base_effort
    return esc_tier, (esc_effort if esc_effort is not None else base_effort)


# The keys `_load_agent` actually reads, plus `description` (human-facing, used by the scaffold's
# agents). Anything else is warned about — see the loop in _load_agent.
_AGENT_KEYS = {"tier", "effort", "escalate", "skills", "tools", "env", "returns", "description"}


def _load_agent(name: str, ctx: _Ctx) -> AgentSpec:
    path = ctx.workspace_dir / "agents" / f"{name}.yaml"
    if not path.is_file():
        _err(f"agent {name!r} is referenced but agents/{name}.yaml does not exist", ctx.file)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        _err(f"agent {name!r}: invalid YAML: {exc}", str(path))
    if not isinstance(doc, dict):
        _err(f"agent {name!r}: file is not a mapping", str(path))

    # An agent file is a config, not a prompt — the only keys cairn reads are the ones below
    # (`description:` is a harmless human-facing convention the scaffold uses). Any other key is
    # a silent-data-loss trap: an author who writes `prompt:`/`mission:` there thinks the agent
    # reads it; cairn ignores it. Warn (never error — an unknown key breaks nothing) so the loss
    # is visible. Behavior belongs in skills, not agent config.
    for key in doc:
        if key not in _AGENT_KEYS:
            ctx.warnings.append(
                Finding(
                    "warning",
                    f"agent {name!r}: unknown key {key!r} in agents/{name}.yaml — cairn ignores "
                    f"it (agent files are config, not prompts; behavior lives in skills)",
                )
            )

    tier = doc.get("tier", "balanced")
    if tier not in TIERS:
        _err(f"agent {name!r}: unknown tier {tier!r} (valid: {list(TIERS)})", str(path))
    effort = doc.get("effort")
    if effort is not None and effort not in EFFORTS:
        _err(f"agent {name!r}: unknown effort {effort!r} (valid: {list(EFFORTS)})", str(path))
    tier, effort = _apply_escalate(doc, name, tier, effort, ctx, str(path))

    skills = tuple(doc.get("skills", []) or [])
    for skill in skills:
        if not (ctx.workspace_dir / "skills" / skill / "SKILL.md").is_file():
            ctx.warnings.append(
                Finding("warning", f"agent {name!r} lists skill {skill!r} but skills/{skill}/SKILL.md is missing")
            )

    tools = doc.get("tools", {}) or {}
    allow = tuple(tools.get("allow", []) or [])
    bash = tools.get("bash")
    if bash:
        _check_allowlist_fragment(bash, name, ctx, str(path))
    network = bool(tools.get("network", False))

    env = tuple(doc.get("env", []) or [])
    for secret in env:
        if secret not in ctx.config.secrets:
            _err(f"agent {name!r}: env {secret!r} is not declared in cairn.toml [secrets]", str(path))

    returns = doc.get("returns", "schemas/step-return.json")
    return AgentSpec(
        name=name,
        tier=tier,
        effort=effort,
        skills=skills,
        tools_allow=allow,
        bash_fragment=bash,
        env=env,
        network=network,
        returns=returns,
    )


# --------------------------------------------------------------------------- #
# Step 5 (part) — the template mini-language, verified against known names.
# --------------------------------------------------------------------------- #

_HELPER_RE = re.compile(r"(\w+)\((.*)\)")


def _check_value_body(body: str, allow_cycle: bool, label: str, ctx: _Ctx, strict: bool) -> None:
    if body in ("pipeline", "date", "datetime"):
        return
    if body == "cycle":
        if not allow_cycle:
            _err(f"{label}: {{cycle}} is only valid inside a loop body", ctx.file)
        return
    if body.startswith("params."):
        key = body[len("params.") :]
        if key not in ctx.params:
            _err(f"{label}: unknown param placeholder {{params.{key}}}", ctx.file)
        return
    if body.startswith("dims."):
        key = body[len("dims.") :]
        if key not in ctx.dims:
            _err(f"{label}: unknown dim placeholder {{dims.{key}}}", ctx.file)
        return
    if strict:
        _err(f"{label}: unknown placeholder {{{body}}}", ctx.file)
    # lenient: a brace that isn't a cairn placeholder (e.g. a shell/Python literal) is left alone.


def _check_helper(raw: str, allow_cycle: bool, label: str, ctx: _Ctx) -> None:
    match = _HELPER_RE.fullmatch(raw)
    if not match:
        return
    first = match.group(2).split(",")[0].strip()
    if first:
        _check_value_body(first, allow_cycle, label, ctx, strict=True)


def _scan_check(text: str, *, strict: bool, allow_cycle: bool, allow_refs: bool, label: str, ctx: _Ctx) -> None:
    """Verify every placeholder in ``text`` resolves.

    ``strict`` (run_id, artifact paths) rejects any unrecognised placeholder; lenient
    (run:/manual: commands, args — which embed shell/Python that may contain literal
    braces) only validates the placeholders that are unambiguously cairn's.
    """
    for ph in scan(text):
        if ph.kind == "helper":
            _check_helper(ph.raw, allow_cycle, label, ctx)
        elif ph.kind == "reference":
            rtype, rname = ph.ref_type, ph.ref_name
            if rtype not in ("artifact", "gate", "run_dir"):
                if strict:
                    _err(f"{label}: unknown placeholder {{{ph.raw}}}", ctx.file)
                continue  # lenient: not a cairn reference, leave it be
            if not allow_refs:
                _err(f"{label}: {{{rtype}:{rname}}} is not allowed here", ctx.file)
            if rtype == "artifact" and rname not in ctx.artifact_names:
                _err(f"{label}: unknown artifact {{artifact:{rname}}}", ctx.file)
            if rtype == "gate" and rname not in ctx.gate_names:
                _err(f"{label}: unknown gate {{gate:{rname}}}", ctx.file)
        else:
            _check_value_body(ph.raw, allow_cycle, label, ctx, strict=strict)


# --------------------------------------------------------------------------- #
# Node parsing — one raw node → one frozen Node (kept nodes only).
# --------------------------------------------------------------------------- #

_STEP_KEYS = {
    "id", "agent", "run", "manual", "args", "needs", "needs_optional", "produces",
    "when", "unless", "timeout", "retry", "executor", "skippable",
}
_GATE_KEYS = {"gate", "when", "unless", "reads", "ask", "options", "default"}
_PARALLEL_KEYS = {"parallel", "on_fail", "when", "unless", "steps"}
_LOOP_KEYS = {"loop", "when", "unless", "min", "max", "until", "on_cap", "body"}


def _reject_unknown(raw: dict, known: set[str], label: str, ctx: _Ctx) -> None:
    unknown = [k for k in raw if k not in known]
    if unknown:
        _err(f"{label}: unknown key(s) {unknown} (valid: {sorted(known)})", ctx.file)


def _classify(raw: dict) -> str:
    if "gate" in raw:
        return "gate"
    if "parallel" in raw:
        return "parallel"
    if "loop" in raw:
        return "loop"
    return "step"


def _node_label(raw: dict, kind: str) -> str:
    ident = raw.get({"step": "id", "gate": "gate", "parallel": "parallel", "loop": "loop"}[kind])
    return f"{kind} {ident!r}"


def _parse_timeout(raw: dict, ctx: _Ctx) -> int:
    if "timeout" not in raw:
        return ctx.config.defaults.step_timeout_s
    from cairn.kernel.config import parse_duration

    try:
        return parse_duration(str(raw["timeout"]))
    except ValueError as exc:
        _err(f"step {raw.get('id')!r}: timeout: {exc}", ctx.file)


def _parse_retry(raw: dict, ctx: _Ctx) -> tuple[int, bool]:
    r = raw.get("retry", {}) or {}
    if not isinstance(r, dict):
        _err(f"step {raw.get('id')!r}: retry must be a mapping", ctx.file)
    attempts = r.get("attempts", 0)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        _err(f"step {raw.get('id')!r}: retry.attempts must be a non-negative integer", ctx.file)
    return attempts, bool(r.get("feedback", False))


def _parse_step(raw: dict, ctx: _Ctx, in_loop: bool, when_runtime: Expr | None) -> StepNode:
    sid = raw.get("id")
    if not isinstance(sid, str) or not sid:
        _err("a step needs a non-empty string 'id'", ctx.file)
    _reject_unknown(raw, _STEP_KEYS, f"step {sid!r}", ctx)

    kinds = [k for k in ("agent", "run", "manual") if k in raw]
    if len(kinds) != 1:
        _err(f"step {sid!r} must have exactly one of agent:/run:/manual:", ctx.file)
    kind_key = kinds[0]

    agent: AgentSpec | None = None
    command: str | None = None
    if kind_key == "agent":
        kind = "agent"
        agent = _load_agent(raw["agent"], ctx)
    else:
        kind = kind_key
        command = raw[kind_key]
        if not isinstance(command, str):
            _err(f"step {sid!r}: {kind_key}: must be a string", ctx.file)

    args = raw.get("args", {}) or {}
    if not isinstance(args, dict):
        _err(f"step {sid!r}: args must be a mapping", ctx.file)

    label = f"step {sid!r} {kind}"
    if command is not None:
        _scan_check(command, strict=False, allow_cycle=in_loop, allow_refs=True, label=label, ctx=ctx)
    for key, value in args.items():
        if isinstance(value, str):
            _scan_check(value, strict=False, allow_cycle=in_loop, allow_refs=True, label=f"step {sid!r} args.{key}", ctx=ctx)

    return StepNode(
        id=sid,
        kind=kind,
        agent=agent,
        command=command,
        args=dict(args),
        needs=tuple(raw.get("needs", []) or []),
        needs_optional=tuple(raw.get("needs_optional", []) or []),
        produces=tuple(raw.get("produces", []) or []),
        when_runtime=when_runtime,
        timeout_s=_parse_timeout(raw, ctx),
        retry=_parse_retry(raw, ctx),
        skippable=bool(raw.get("skippable", False)),
        executor=raw.get("executor"),
        tier=agent.tier if agent else None,
        effort=agent.effort if agent else None,
        env=agent.env if agent else (),
        network=agent.network if agent else False,
    )


def _parse_gate(raw: dict, ctx: _Ctx, when_runtime: Expr | None) -> GateNode:
    name = raw["gate"]
    _reject_unknown(raw, _GATE_KEYS, f"gate {name!r}", ctx)
    opts_raw = raw.get("options", {}) or {}
    if not isinstance(opts_raw, dict) or not opts_raw:
        _err(f"gate {name!r} needs a non-empty 'options' mapping", ctx.file)
    default = raw.get("default")
    if default is None:
        _err(f"gate {name!r} needs a 'default'", ctx.file)
    if default not in opts_raw:
        _err(f"gate {name!r}: default {default!r} is not one of the options {list(opts_raw)}", ctx.file)
    return GateNode(
        name=name,
        reads=tuple(raw.get("reads", []) or []),
        ask=str(raw.get("ask", "")),
        options=tuple((k, str(v)) for k, v in opts_raw.items()),
        default=str(default),
        when_runtime=when_runtime,
    )


def _parse_parallel(raw: dict, ctx: _Ctx, when_runtime: Expr | None, children: list[Node]) -> ParallelNode:
    name = raw["parallel"]
    _reject_unknown(raw, _PARALLEL_KEYS, f"parallel {name!r}", ctx)
    on_fail = raw.get("on_fail", "wait_all")
    if on_fail not in ("wait_all", "fast"):
        _err(f"parallel {name!r}: on_fail must be 'wait_all' or 'fast', got {on_fail!r}", ctx.file)
    for child in children:
        if not isinstance(child, StepNode):
            _err(f"parallel {name!r}: children must be steps", ctx.file)
    return ParallelNode(name=name, on_fail=on_fail, steps=tuple(children), when_runtime=when_runtime)


def _parse_loop(raw: dict, ctx: _Ctx, when_runtime: Expr | None, children: list[Node]) -> LoopNode:
    name = raw["loop"]
    _reject_unknown(raw, _LOOP_KEYS, f"loop {name!r}", ctx)
    minimum = raw.get("min", 1)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
        _err(f"loop {name!r}: min must be a non-negative integer", ctx.file)
    max_raw = raw.get("max", {}) or {}
    if not isinstance(max_raw, dict):
        _err(f"loop {name!r}: max must be a mapping with interactive/headless", ctx.file)
    mi = max_raw.get("interactive", minimum)
    mh = max_raw.get("headless", minimum)
    for tag, val in (("interactive", mi), ("headless", mh)):
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            _err(f"loop {name!r}: max.{tag} must be a non-negative integer", ctx.file)
    until = None
    if raw.get("until") is not None:
        until = _parse_cond(str(raw["until"]), f"loop {name!r} until", ctx)
        _static_check_paths(until, f"loop {name!r} until", ctx)
    on_cap = raw.get("on_cap", "halt")
    if on_cap not in ("halt", "continue"):
        _err(f"loop {name!r}: on_cap must be 'halt' or 'continue', got {on_cap!r}", ctx.file)
    return LoopNode(
        name=name,
        min=minimum,
        max_interactive=mi,
        max_headless=mh,
        until=until,
        on_cap=on_cap,
        body=tuple(children),
        when_runtime=when_runtime,
    )


def _build(raw_nodes: list, ctx: _Ctx, in_loop: bool) -> tuple[list[Node], list[PlanSkip]]:
    active: list[Node] = []
    skips: list[PlanSkip] = []
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            _err(f"each node must be a mapping, got {raw!r}", ctx.file)
        kind = _classify(raw)
        label = _node_label(raw, kind)
        decision, when_runtime, reason = _expand_condition(raw, label, ctx)
        ident = raw.get({"step": "id", "gate": "gate", "parallel": "parallel", "loop": "loop"}[kind])
        if decision == "drop":
            skips.append(PlanSkip(node=str(ident), kind=kind, reason=reason or ""))
            continue
        if kind == "step":
            active.append(_parse_step(raw, ctx, in_loop, when_runtime))
        elif kind == "gate":
            active.append(_parse_gate(raw, ctx, when_runtime))
        elif kind == "parallel":
            children, cskips = _build(raw.get("steps", []) or [], ctx, in_loop=False)
            active.append(_parse_parallel(raw, ctx, when_runtime, children))
            skips.extend(cskips)
        else:  # loop
            children, cskips = _build(raw.get("body", []) or [], ctx, in_loop=True)
            active.append(_parse_loop(raw, ctx, when_runtime, children))
            skips.extend(cskips)
    return active, skips


# --------------------------------------------------------------------------- #
# Step 4 — dataflow verification.
# --------------------------------------------------------------------------- #


def _gather(raw_nodes: list) -> tuple[dict[str, list[str]], set[str]]:
    """Every producer in the whole pipeline (dropped or not) → name → [node ids]; plus the
    set of all gate names. Powers candidate-producer diagnostics, the ``needs_optional``
    declared-set, and the unused-artifact warning."""
    producers: dict[str, list[str]] = {}
    gates: set[str] = set()

    def visit(rn: Any) -> None:
        if not isinstance(rn, dict):
            return
        if "gate" in rn:
            gates.add(rn["gate"])
            producers.setdefault(rn["gate"], []).append(str(rn["gate"]))
        elif "parallel" in rn:
            for child in rn.get("steps", []) or []:
                visit(child)
        elif "loop" in rn:
            for child in rn.get("body", []) or []:
                visit(child)
        else:
            for name in rn.get("produces", []) or []:
                producers.setdefault(name, []).append(str(rn.get("id")))

    for rn in raw_nodes:
        visit(rn)
    return producers, gates


def _did_you_mean(name: str, ctx: _Ctx) -> str:
    cands = difflib.get_close_matches(name, sorted(ctx.artifact_names | ctx.gate_names), n=3)
    return f" (did you mean {cands}?)" if cands else ""


def _add_produce(name: str, produced: set[str], in_loop: bool, node_id: str, ctx: _Ctx) -> None:
    if name not in (ctx.artifact_names | ctx.gate_names):
        _err(
            f"step {node_id!r} produces {name!r}, which is not a declared artifact or gate"
            f"{_did_you_mean(name, ctx)}",
            ctx.file,
        )
    if name in produced and not in_loop:
        _err(f"artifact {name!r} is produced more than once (by {node_id!r} and an earlier step)", ctx.file)
    produced.add(name)


def _check_needs(node: StepNode, available: set[str], producers: dict[str, list[str]], ctx: _Ctx) -> None:
    declared = ctx.artifact_names | ctx.gate_names
    for name in node.needs:
        if name not in declared:
            _err(
                f"step {node.id!r} needs {name!r}, which is not a declared artifact or gate"
                f"{_did_you_mean(name, ctx)}",
                ctx.file,
            )
        if name not in available:
            cands = [c for c in producers.get(name, []) if c != node.id]
            hint = (
                f" (produced by {cands} — dropped by a conditional, or out of order?)"
                if cands
                else f" (no step produces {name!r})"
            )
            _err(f"step {node.id!r} needs {name!r}, which is not produced before it{hint}", ctx.file)
    for name in node.needs_optional:
        if name not in declared:
            _err(
                f"step {node.id!r} needs_optional {name!r} is not a declared artifact or gate"
                f"{_did_you_mean(name, ctx)}",
                ctx.file,
            )


def _produce_path_has_cycle(path: str) -> bool:
    """Whether an artifact path template's placeholders reference ``{cycle}`` (the SAME
    scanner ``_scan_check``/``_check_value_body`` use to recognise the placeholder)."""
    return any(ph.kind == "value" and ph.raw == "cycle" for ph in scan(path))


def _verify_dataflow(
    nodes: list[Node],
    producers: dict[str, list[str]],
    ctx: _Ctx,
    artifact_decls: dict[str, ArtifactDecl],
) -> None:
    produced: set[str] = set()
    for node in nodes:
        if isinstance(node, StepNode):
            _check_needs(node, produced, producers, ctx)
            for name in node.produces:
                _add_produce(name, produced, in_loop=False, node_id=node.id, ctx=ctx)
        elif isinstance(node, GateNode):
            for name in node.reads:
                if name not in produced:
                    _err(f"gate {node.name!r} reads {name!r}, which is not produced before it", ctx.file)
            _add_produce(node.name, produced, in_loop=False, node_id=node.name, ctx=ctx)
        elif isinstance(node, ParallelNode):
            snapshot = set(produced)  # siblings run concurrently — none can feed another
            seen: dict[str, str] = {}
            for child in node.steps:
                for name in child.produces:
                    if name in seen:
                        _err(
                            f"parallel {node.name!r}: {name!r} is produced by both {seen[name]!r} "
                            f"and {child.id!r} (children must produce disjoint artifacts)",
                            ctx.file,
                        )
                    seen[name] = child.id
            for child in node.steps:
                _check_needs(child, snapshot, producers, ctx)
            for child in node.steps:
                for name in child.produces:
                    _add_produce(name, produced, in_loop=False, node_id=child.id, ctx=ctx)
        elif isinstance(node, LoopNode):
            # The sanctioned re-production (§2.6) is a body step re-writing an artifact that
            # existed BEFORE the loop. A brand-new name introduced twice inside the body is a
            # real duplicate — so exempt only names present at loop entry.
            before_loop = set(produced)
            loop_new: dict[str, str] = {}
            for child in node.body:
                names = child.produces if isinstance(child, StepNode) else (child.name,)
                cid = child.id if isinstance(child, StepNode) else child.name
                if isinstance(child, StepNode):
                    _check_needs(child, produced, producers, ctx)
                for name in names:
                    if name in before_loop:
                        produced.add(name)  # sanctioned re-production
                    elif name in loop_new:
                        _err(
                            f"loop {node.name!r}: {name!r} is produced by both {loop_new[name]!r} "
                            f"and {cid!r} within the body (only re-production of an artifact "
                            f"produced before the loop is allowed)",
                            ctx.file,
                        )
                    else:
                        loop_new[name] = cid
                        produced.add(name)
                        # codex-F3 (critical): a NEW artifact first produced inside a loop body
                        # is what _completed_cycles (walk.py) re-renders per cycle to discover
                        # how many cycles already completed. If its path never varies by
                        # {cycle}, every cycle renders the identical file — once it validates,
                        # nothing can ever tell cycle N from cycle N+1 apart, so cycle discovery
                        # cannot terminate on its own (walk.py's `while True`). A re-produced
                        # BEFORE-loop artifact (the branch above) is exempt on purpose: it is
                        # the same ArtifactDecl used outside the loop too, where no cycle exists
                        # to substitute — forcing {cycle} into its path would break rendering
                        # there, not just here.
                        decl = artifact_decls.get(name)
                        if decl is not None and not _produce_path_has_cycle(decl.path):
                            _err(
                                f"loop {node.name!r}: step {cid!r} produces {name!r} whose path "
                                f"{decl.path!r} does not reference {{cycle}} — every cycle would "
                                "render the same artifact and the loop could never detect how "
                                "many cycles have completed",
                                ctx.file,
                            )


# --------------------------------------------------------------------------- #
# Step 4 (part) — conditional-gate consumer lint.
#
# A gate carrying `when:` is dropped (or runtime-inactive) whenever its condition
# is false. A node that consumes that gate — `{gate:x}` in a run/manual command or
# args value, or `gates.x` in a when/unless/until expr — then dies at runtime with
# a TemplateError/EvalError (a CONFIG halt) unless it is guarded by the SAME
# condition. This lint proves that guard at plan time: an unguarded consumer is a
# hard error, a consumer whose guard can't be shown to include the gate's condition
# is a warning. Mode-independent (over raw nodes), so it fires even in a param set
# where the gate happens to be kept.
# --------------------------------------------------------------------------- #


def _normalize_cond(src: str) -> str:
    """Whitespace-stripped form for the conjunct-containment approximation."""
    return re.sub(r"\s+", "", src)


def _conditional_gate_whens(raw_nodes: list) -> dict[str, str]:
    """Every gate carrying a non-empty ``when:`` → its raw condition string (whole pipeline)."""
    out: dict[str, str] = {}

    def visit(rn: Any) -> None:
        if not isinstance(rn, dict):
            return
        if "gate" in rn:
            w = rn.get("when")
            if isinstance(w, str) and w.strip():
                out[str(rn["gate"])] = w
        elif "parallel" in rn:
            for c in rn.get("steps", []) or []:
                visit(c)
        elif "loop" in rn:
            for c in rn.get("body", []) or []:
                visit(c)

    for rn in raw_nodes:
        visit(rn)
    return out


def _gates_consumed(rn: dict) -> set[str]:
    """The gate names a raw node references: ``{gate:X}`` in its run/manual command and args
    string values, plus ``gates.X`` in its when/unless/until expressions."""
    gates: set[str] = set()
    texts: list[str] = [rn[k] for k in ("run", "manual") if isinstance(rn.get(k), str)]
    args = rn.get("args")
    if isinstance(args, dict):
        texts.extend(v for v in args.values() if isinstance(v, str))
    for text in texts:
        for ph in scan(text):
            if ph.kind == "reference" and ph.ref_type == "gate" and ph.ref_name:
                gates.add(ph.ref_name)
    for key in ("when", "unless", "until"):
        src = rn.get(key)
        if isinstance(src, str) and src.strip():
            try:
                expr = parse_expr(src)
            except ExprError:
                continue  # a malformed expr is reported by the normal parse path
            for root, parts in expr.paths():
                if root == "gates" and parts:
                    gates.add(parts[0])
    return gates


def _lint_conditional_gates(raw_nodes: list, ctx: _Ctx) -> None:
    """Verify every consumer of a conditional gate is guarded by the gate's own condition.

    For a consumer, the effective guard is the conjunction of its own ``when:`` and every
    enclosing container's ``when:`` (a loop/parallel body step inherits its container's guard).
    A consumer with *no* effective guard that touches a conditional gate is a hard error; one
    whose guard cannot be shown to contain the gate's condition is a warning.

    The conjunct test is normalized-string CONTAINMENT (whitespace-stripped) — a documented
    approximation. It can be fooled by a re-parenthesized/reordered condition or a coincidental
    substring inside a string literal, but it only ever *under*-reports (downgrading an error to
    a warning, or a warning to silence) — it never raises a false error on a truly-guarded
    consumer, because an exact same-string guard always contains itself.
    """
    gate_whens = _conditional_gate_whens(raw_nodes)
    if not gate_whens:
        return

    def visit(rn: Any, ancestors: tuple[str, ...]) -> None:
        if not isinstance(rn, dict):
            return
        own = rn.get("when")
        guards = ancestors + ((own,) if isinstance(own, str) and own.strip() else ())
        label = _node_label(rn, _classify(rn))
        for gate in sorted(_gates_consumed(rn)):
            cond = gate_whens.get(gate)
            if cond is None:
                continue  # consuming an unconditional gate is always safe
            if not guards:
                _err(
                    f"{label} consumes conditional gate {gate!r} (active only while its "
                    f"when: {cond!r} holds) but is itself unconditional — it will break at "
                    f"runtime whenever the gate is inactive. Guard it with the same when:.",
                    ctx.file,
                )
            elif not any(_normalize_cond(cond) in _normalize_cond(g) for g in guards):
                ctx.warnings.append(
                    Finding(
                        "warning",
                        f"{label} consumes conditional gate {gate!r} (when: {cond}) but its "
                        f"guard does not contain that condition as a conjunct — cairn can't prove "
                        f"the consumer is inactive whenever the gate is; guard it with the gate's "
                        f"condition to be safe.",
                    )
                )
        if "parallel" in rn:
            for c in rn.get("steps", []) or []:
                visit(c, guards)
        elif "loop" in rn:
            for c in rn.get("body", []) or []:
                visit(c, guards)

    for rn in raw_nodes:
        visit(rn, ())


# --------------------------------------------------------------------------- #
# Step 5 (part) — guards.
# --------------------------------------------------------------------------- #

# Kept here (not in guards.py) so this plan-time check and guards.py's build_shims share
# ONE implementation — guards.py already imports GuardDecl from this module, so importing
# _binary_name the other way round (plan.py → guards.py) would be circular.
_GLOB_STOP = set(" \t*?[")


def _binary_name(pattern: str) -> str:
    """The real binary a ``match_command`` guards: its literal prefix up to the first
    glob metachar or space. ``"brease* createMedia*"`` → ``"brease"``."""
    out: list[str] = []
    for ch in pattern:
        if ch in _GLOB_STOP:
            break
        out.append(ch)
    return "".join(out)


def _parse_guards(raw_guards: Any, ctx: _Ctx) -> list[GuardDecl]:
    if not raw_guards:
        return []
    if not isinstance(raw_guards, list):
        _err("guards: must be a list", ctx.file)
    out: list[GuardDecl] = []
    for g in raw_guards:
        name = g.get("name")
        if not name:
            _err("a guard needs a 'name'", ctx.file)
        match = g.get("match", {}) or {}
        match_command = str(match.get("command", ""))
        check_rel = g.get("check")
        if not check_rel:
            _err(f"guard {name!r} needs a 'check' script", ctx.file)
        check_path = ctx.workspace_dir / check_rel
        if not check_path.is_file():
            _err(f"guard {name!r}: check script {check_rel!r} not found", ctx.file)
        on_error = g.get("on_error", "deny")
        if on_error not in ("allow", "deny"):
            _err(f"guard {name!r}: on_error must be 'allow' or 'deny', got {on_error!r}", ctx.file)

        enforce = tuple(g.get("enforce", []) or [])
        if "shim" in enforce:
            # codex-F4 (critical): build_shims derives the shimmed binary from this same
            # prefix (guards.py:_binary_name) and does `shim_dir / binary` — a binary with a
            # '/' collapses that join to an absolute path (Path("/a") / "/b" == Path("/b")),
            # landing an executable OUTSIDE the shim dir at plan/run build time. Reject here,
            # before any file is written, rather than let build_shims discover it.
            binary = _binary_name(match_command)
            if not binary or "/" in binary or binary in (".", ".."):
                _err(
                    f"guard {name!r}: match_command {match_command!r} has binary {binary!r}, "
                    "not a bare command name (no path separators, no '.'/'..' segments, not "
                    "empty) — a shim can only be built for a plain command on PATH",
                    ctx.file,
                )

        when_runtime = None
        src = g.get("when")
        if src is not None:
            expr = _parse_cond(str(src), f"guard {name!r} when", ctx)
            _static_check_paths(expr, f"guard {name!r} when", ctx)
            value = _cond_value(expr, f"guard {name!r} when", ctx)
            if value is _DEFER:
                when_runtime = expr
            elif not bool(value):
                continue  # params/dims render this guard inactive — drop it

        out.append(
            GuardDecl(
                name=name,
                match_tool=str(match.get("tool", "")),
                match_command=match_command,
                check=check_path,
                enforce=enforce,
                on_error=on_error,
                when=when_runtime,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Step 5 (part) — run_id + artifact paths.
# --------------------------------------------------------------------------- #


def _validate_run_id(run_id: str, ctx: _Ctx, now: datetime) -> None:
    for ph in scan(run_id):
        if ph.kind == "reference" and ph.ref_type in ("artifact", "gate", "run_dir"):
            _err("run_id may not use references ({artifact:}/{gate:}/{run_dir})", ctx.file)
        if ph.kind == "value" and ph.raw == "cycle":
            _err("run_id may not use {cycle} (there is no loop at run-id time)", ctx.file)
    try:
        render(run_id, TemplateContext(params=ctx.params, dims=ctx.dims, pipeline=None, now=now))
    except TemplateError as exc:
        # {pipeline} is fine in a run_id but not resolvable here — tolerate only that.
        if "{pipeline}" not in str(exc):
            _err(f"run_id: {exc}", ctx.file)


# --------------------------------------------------------------------------- #
# Step 6 — slice + lazy executor resolution + emit.
# --------------------------------------------------------------------------- #


def _node_id(node: Node) -> str:
    if isinstance(node, StepNode):
        return node.id
    return node.name


def _resolve_exec(node: StepNode, executor_arg: str | None, step_executors: dict[str, str] | None, config: Config, file: str) -> tuple[str, str, str | None]:
    pin = node.executor
    exec_name = pin or (step_executors or {}).get(node.id) or executor_arg or config.workspace.default_executor
    if not exec_name:
        _err(
            f"step {node.id!r} is an agent step but no executor is configured "
            f"(set --executor, a per-step executor, or [workspace].default_executor)",
            file,
        )
    ec = config.executors.get(exec_name)
    if ec is None:
        _err(f"step {node.id!r}: executor {exec_name!r} is not defined in cairn.toml", file)
    if not ec.enabled:
        _err(f"step {node.id!r}: executor {exec_name!r} is disabled in cairn.toml", file)
    tier_spec = ec.tiers.get(node.tier)
    if tier_spec is None:
        _err(f"step {node.id!r}: executor {exec_name!r} has no model mapped for tier {node.tier!r}", file)
    # Effort precedence (specific over general): the agent's own `effort:` wins; the tier
    # spec's effort is the fallback when the agent pins none; None last (the executor applies
    # its own default). A per-agent effort must never be silently overridden by a tier-baked one.
    effort = node.effort if node.effort is not None else tier_spec.effort
    return exec_name, tier_spec.model, effort


def _emit(
    active: list[Node],
    executor_arg: str | None,
    step_executors: dict[str, str] | None,
    config: Config,
    from_node: str | None,
    to_node: str | None,
    file: str,
) -> tuple[tuple[Node, ...], dict[str, tuple[str, str, str | None]]]:
    ids = [_node_id(n) for n in active]
    start, end = 0, len(active)
    if from_node is not None:
        if from_node not in ids:
            _err(f"--from names unknown node {from_node!r} (nodes: {ids})", file)
        start = ids.index(from_node)
    if to_node is not None:
        if to_node not in ids:
            _err(f"--to names unknown node {to_node!r} (nodes: {ids})", file)
        end = ids.index(to_node) + 1
    if start >= end:
        _err(f"--from {from_node!r} comes after --to {to_node!r} — empty range", file)
    sliced = active[start:end]

    resolved_models: dict[str, tuple[str, str, str | None]] = {}

    def resolve(node: Node) -> Node:
        if isinstance(node, StepNode):
            if node.kind == "agent":
                exec_name, model, effort = _resolve_exec(node, executor_arg, step_executors, config, file)
                resolved_models[node.id] = (exec_name, model, effort)
                return replace(node, executor=exec_name)
            return node
        if isinstance(node, ParallelNode):
            return replace(node, steps=tuple(resolve(c) for c in node.steps))
        if isinstance(node, LoopNode):
            return replace(node, body=tuple(resolve(c) for c in node.body))
        return node

    return tuple(resolve(n) for n in sliced), resolved_models


# --------------------------------------------------------------------------- #
# Range-scoped tool preflight (docs/TOOLING-AND-GROWTH §2).
#
# A `[tools]` entry may carry `needed_by = [step-or-pipeline, …]`, scoping it to
# the steps that care. At plan time — offline, so we NEVER run a tool's `check`;
# that stays `cairn doctor`'s job — we warn (never error) when an in-range step
# (or this pipeline) is named by a tool's scope: fail-fast beats a crash after a
# long build. A `needed_by` naming no step/pipeline anywhere in the workspace is
# a dangling-scope lint on the same warnings channel. Unscoped tools (no
# `needed_by`) are workspace-global — doctor's concern, not a range warning.
# --------------------------------------------------------------------------- #


def _flatten_step_ids(nodes: tuple[Node, ...] | list[Node]) -> set[str]:
    """Every StepNode id in ``nodes``, descending into parallel/loop children."""
    out: set[str] = set()
    for node in nodes:
        if isinstance(node, StepNode):
            out.add(node.id)
        elif isinstance(node, ParallelNode):
            out |= _flatten_step_ids(node.steps)
        elif isinstance(node, LoopNode):
            out |= _flatten_step_ids(node.body)
    return out


def _collect_raw_step_ids(raw_nodes: Any, into: set[str]) -> None:
    """Every step ``id`` in a raw (unparsed) node list — incl. parallel/loop bodies and
    conditionally-dropped steps — the full set a ``needed_by`` scope may legitimately name."""
    if not isinstance(raw_nodes, list):
        return
    for rn in raw_nodes:
        if not isinstance(rn, dict):
            continue
        if "parallel" in rn:
            _collect_raw_step_ids(rn.get("steps", []) or [], into)
        elif "loop" in rn:
            _collect_raw_step_ids(rn.get("body", []) or [], into)
        elif "gate" not in rn:
            sid = rn.get("id")
            if isinstance(sid, str) and sid:
                into.add(sid)


def _workspace_scope_names(workspace_dir: Path) -> set[str]:
    """Every pipeline name + every step id across the workspace's pipelines — the universe a
    tool's ``needed_by`` may name. Tolerant of unreadable/malformed files (the real planner
    reports those precisely); used only to distinguish a valid cross-pipeline scope from a
    dangling one, so a tool needed by a step in *another* pipeline is never mis-flagged.

    This is the LAZY fallback: :func:`_lint_tools` matches every target against the current
    pipeline's local universe first and only pays this workspace-wide re-parse for residual
    targets, so the common in-pipeline-scoping case never touches sibling files (and doctor,
    which plans every pipeline, stays O(N) rather than O(N²) parses)."""
    names: set[str] = set()
    d = workspace_dir / "pipelines"
    if not d.is_dir():
        return names
    for f in sorted(d.glob("*.yaml")):
        names.add(f.stem)
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(doc, dict):
            _collect_raw_step_ids(doc.get("steps", []) or [], names)
    return names


def _lint_tools(
    config: Config,
    pipeline_name: str,
    requirements: tuple[ToolRequirement, ...],
    local_names: set[str],
    workspace_names: Callable[[], set[str]],
    warnings: list[Finding],
) -> None:
    """Append the tool warnings for one plan (see the section note above): the dangling-scope
    lint (from the raw ``[tools]`` declarations) plus the range-scoped "unverified here"
    warnings — the latter derived FROM ``requirements`` (the exact set ``cairn run`` hard-stops
    on), so warning-set == hard-stop-set holds by construction, not by parallel logic.

    ``local_names`` is the current pipeline's own universe (its name + all raw step ids,
    already parsed); ``workspace_names`` is the lazy workspace-wide fallback, invoked at
    most once and only when a target fails to match locally."""
    fallback: set[str] | None = None
    for tool in config.tools.values():
        for target in tool.needed_by:
            if target in local_names:
                continue
            if fallback is None:
                fallback = workspace_names()
            if target not in fallback:
                warnings.append(
                    Finding(
                        "warning",
                        f"tool {tool.name!r}: needed_by names {target!r}, which is not a step "
                        f"or pipeline anywhere in this workspace (dangling scope)",
                    )
                )
    for req in requirements:
        for target in req.targets:
            what = "pipeline" if target == pipeline_name else "step"
            warnings.append(
                Finding(
                    "warning",
                    f"{what} {target!r} needs tool {req.tool!r} — declared but unverified here; "
                    f"run cairn doctor",
                )
            )


def _tool_requirements(
    config: Config, pipeline_name: str, in_range_ids: set[str]
) -> tuple[ToolRequirement, ...]:
    """The range-scoped tool subset for the run-time hard-stop, captured structurally so
    ``cairn run`` can probe each ``check`` before minting anything — and the single source
    :func:`_lint_tools` derives its "unverified here" warnings from. A tool is included iff its
    ``needed_by`` names an in-range step or this pipeline; unscoped tools (empty ``needed_by``)
    and tools scoped only to out-of-range / dropped steps are excluded — the exclusion is what
    makes "out-of-range ⇒ never checked" hold. ``targets`` is the sorted in-range step ids, plus
    the pipeline name when pipeline-scoped (deduped for the degenerate step-named-as-pipeline
    case)."""
    reqs: list[ToolRequirement] = []
    for tool in config.tools.values():
        scoped = set(tool.needed_by)
        targets = sorted(in_range_ids & scoped)
        if pipeline_name in scoped and pipeline_name not in targets:
            targets.append(pipeline_name)
        if targets:
            reqs.append(ToolRequirement(tool.name, tool.check, tool.install, tuple(targets)))
    return tuple(reqs)


# --------------------------------------------------------------------------- #
# The public entry points.
# --------------------------------------------------------------------------- #

_KNOWN_TOP_LEVEL = {"pipeline", "version", "params", "dims", "run_id", "artifacts", "guards", "steps"}


def plan(
    workspace_dir: Path,
    pipeline_name: str,
    params: dict[str, str],
    *,
    executor: str | None = None,
    step_executors: dict[str, str] | None = None,
    now: datetime,
    to_node: str | None = None,
    from_node: str | None = None,
    headless: bool = False,
) -> Plan:
    """Plan ``pipeline_name`` in ``workspace_dir`` with ``params`` → a :class:`Plan`.

    Pure: reads workspace files, resolves everything, and raises :class:`ConfigError`
    (with the offending file) on any problem. ``headless`` is recorded via the loop caps
    (both are kept on the node); it does not change what plans.
    """
    workspace_dir = Path(workspace_dir)
    pfile = workspace_dir / "pipelines" / f"{pipeline_name}.yaml"
    if not pfile.is_file():
        _err(f"no pipeline {pipeline_name!r}: {pfile} not found", str(pfile))
    try:
        doc = yaml.safe_load(pfile.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        _err(f"pipeline {pipeline_name!r} is not valid YAML: {exc}", str(pfile))
    if not isinstance(doc, dict):
        _err(f"pipeline {pipeline_name!r}: file must be a mapping", str(pfile))

    warnings: list[Finding] = []
    for key in doc:
        if key not in _KNOWN_TOP_LEVEL:
            warnings.append(Finding("warning", f"unknown top-level key {key!r} in pipeline — ignored"))

    pipeline = doc.get("pipeline", pipeline_name)
    version = doc.get("version", 1)

    config = load_config(workspace_dir)  # ConfigError on a bad cairn.toml
    warnings.extend(config.warnings)
    # The workspace version pin (DISTRIBUTION §3): refuse to plan when the installed
    # cairn falls outside the workspace's `requires` range.
    check_requires(config.requires, file=workspace_dir / "cairn.toml")

    artifact_decls = parse_artifacts(doc.get("artifacts", {}) or {}, workspace_dir)

    resolved_params = _resolve_params(doc.get("params", {}), params, str(pfile))
    dims = _derive_dims(doc.get("dims", {}), resolved_params, str(pfile))
    resolver = _make_plan_resolver(resolved_params, dims)

    raw_steps = doc.get("steps", []) or []
    if not isinstance(raw_steps, list):
        _err("steps: must be a list", str(pfile))

    producers, gate_names = _gather(raw_steps)
    ctx = _Ctx(
        workspace_dir=workspace_dir,
        config=config,
        file=str(pfile),
        resolver=resolver,
        params=resolved_params,
        dims=dims,
        artifact_names=set(artifact_decls),
        gate_names=gate_names,
        warnings=warnings,
    )

    active, skips = _build(raw_steps, ctx, in_loop=False)
    _verify_dataflow(active, producers, ctx, artifact_decls)
    _lint_conditional_gates(raw_steps, ctx)

    for name in artifact_decls:
        if name not in producers:
            warnings.append(Finding("warning", f"artifact {name!r} is declared but never produced by any step"))

    run_id_template = doc.get("run_id", "{pipeline}-{date}")
    _validate_run_id(run_id_template, ctx, now)
    for decl in artifact_decls.values():
        _scan_check(
            decl.path,
            strict=True,
            allow_cycle=True,
            allow_refs=False,
            label=f"artifact {decl.name!r} path",
            ctx=ctx,
        )

    guards = _parse_guards(doc.get("guards", []), ctx)

    emitted, resolved_models = _emit(active, executor, step_executors, config, from_node, to_node, str(pfile))

    # Range-scoped tool preflight — offline; the workspace-wide scan is a lazy fallback
    # that only runs for a needed_by target the current pipeline can't account for. The
    # requirement set is computed once and feeds BOTH the plan-time warnings and the
    # run-time hard-stop, so the two can never drift.
    tool_requirements: tuple[ToolRequirement, ...] = ()
    if config.tools:
        tool_requirements = _tool_requirements(config, pipeline_name, _flatten_step_ids(emitted))
        local_names = {pipeline_name}
        _collect_raw_step_ids(raw_steps, local_names)
        _lint_tools(
            config,
            pipeline_name,
            tool_requirements,
            local_names,
            lambda: _workspace_scope_names(workspace_dir),
            warnings,
        )

    return Plan(
        pipeline=pipeline,
        version=version,
        params=resolved_params,
        dims=dims,
        run_id_template=run_id_template,
        nodes=emitted,
        artifacts=artifact_decls,
        guards=tuple(guards),
        warnings=warnings,
        executor_default=config.workspace.default_executor,
        resolved_models=resolved_models,
        skipped=tuple(skips),
        tool_requirements=tool_requirements,
    )


def render_run_id(plan_obj: Plan, now: datetime) -> str:
    """Render ``plan_obj.run_id_template`` to the concrete run-dir name (no refs, just
    params/dims/pipeline/date/datetime)."""
    ctx = TemplateContext(params=plan_obj.params, dims=plan_obj.dims, pipeline=plan_obj.pipeline, now=now)
    try:
        return render(plan_obj.run_id_template, ctx)
    except TemplateError as exc:
        _err(f"run_id: {exc}")
