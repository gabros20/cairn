"""The cairn template mini-language (docs/API.md §2.8).

One syntax, used by ``run_id:``, ``artifact.path:``, ``args:`` values, and
``run:`` / ``manual:`` command strings. Two entry points:

- :func:`render` — substitute placeholders against a :class:`TemplateContext`.
- :func:`scan` — enumerate the placeholders for plan-time verification (what
  does this template need?), without resolving anything.

    VALUE      {params.<name>} {dims.<key>} {pipeline} {date} {datetime} {cycle}
    REFERENCE  {artifact:<name>} {gate:<name>} {run_dir}
    HELPERS    {slug(<value>)} {dash(<value>)} {short(<value>, n)}

Rules: a missing value is a :class:`TemplateError` naming the placeholder —
never a silent empty (``acme--20260703`` must be impossible). Helpers are the
fixed set above; there are no user functions. ``{date}`` / ``{datetime}`` format
``ctx.now`` — an injected clock, never ``datetime.now()`` — for determinism and
testability. ``{cycle}`` is only legal when a cycle is bound. Reference
resolvers may be ``None`` at plan time, in which case using such a placeholder
raises; and ``{artifact:…}`` is structurally illegal inside an artifact ``path``
template (``artifact_refs_allowed=False``) because paths cannot depend on paths.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from cairn.kernel.errors import CairnError

HELPERS: frozenset[str] = frozenset({"slug", "dash", "short"})

_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_HELPER_CALL = re.compile(r"(\w+)\((.*)\)")


class TemplateError(CairnError):
    """A template could not be rendered — missing value, unknown or illegal placeholder."""


@dataclass(frozen=True)
class Placeholder:
    """One ``{...}`` occurrence, classified for plan-time verification.

    ``kind`` is ``"value"`` | ``"reference"`` | ``"helper"``. ``raw`` is the text
    inside the braces. For references, ``ref_type`` is ``artifact`` / ``gate`` /
    ``run_dir`` and ``ref_name`` is the target (``None`` for ``run_dir``).
    """

    kind: str
    raw: str
    ref_type: str | None = None
    ref_name: str | None = None


@dataclass
class TemplateContext:
    """The inputs render() draws on. Reference resolvers may be ``None`` at plan time."""

    params: Mapping[str, Any] = field(default_factory=dict)
    dims: Mapping[str, Any] = field(default_factory=dict)
    pipeline: str | None = None
    cycle: int | None = None
    now: datetime | None = None  # INJECTED clock — {date}/{datetime} format from it
    artifact: Callable[[str], str] | None = None  # name -> absolute path
    gate: Callable[[str], str] | None = None       # name -> recorded choice value
    run_dir: str | None = None
    artifact_refs_allowed: bool = True  # False inside an artifact.path template


# --- render ------------------------------------------------------------------

def render(text: str, ctx: TemplateContext) -> str:
    """Render ``text`` against ``ctx``, or raise :class:`TemplateError`."""
    return _PLACEHOLDER.sub(lambda m: _render_one(m.group(1).strip(), ctx), text)


def _render_one(body: str, ctx: TemplateContext) -> str:
    helper = _HELPER_CALL.fullmatch(body)
    if helper and helper.group(1) in HELPERS:
        return _render_helper(helper.group(1), helper.group(2), ctx, body)
    if body == "run_dir":
        return _render_reference("run_dir", None, ctx)
    if ":" in body:
        ref_type, _, ref_name = body.partition(":")
        return _render_reference(ref_type.strip(), ref_name.strip(), ctx)
    return _render_value(body, ctx)


def _render_value(body: str, ctx: TemplateContext) -> str:
    if body == "pipeline":
        if ctx.pipeline is None:
            raise TemplateError("{pipeline}: no pipeline name in context")
        return str(ctx.pipeline)
    if body == "date":
        return _clock(ctx).strftime("%Y%m%d")
    if body == "datetime":
        return _clock(ctx).strftime("%Y%m%d-%H%M")
    if body == "cycle":
        if ctx.cycle is None:
            raise TemplateError("{cycle}: no cycle bound (valid only inside a loop body)")
        return str(ctx.cycle)
    if body.startswith("params."):
        return _lookup(ctx.params, body[len("params."):], body)
    if body.startswith("dims."):
        return _lookup(ctx.dims, body[len("dims."):], body)
    raise TemplateError(f"unknown placeholder {{{body}}}")


def _render_reference(ref_type: str, ref_name: str | None, ctx: TemplateContext) -> str:
    if ref_type == "run_dir":
        if ctx.run_dir is None:
            raise TemplateError("{run_dir} is not available in this context")
        return str(ctx.run_dir)
    if ref_type == "artifact":
        if not ctx.artifact_refs_allowed:
            raise TemplateError(
                f"{{artifact:{ref_name}}} is illegal in an artifact path template "
                "(a path cannot depend on another artifact's path)"
            )
        return _resolve_ref(ctx.artifact, "artifact", ref_name)
    if ref_type == "gate":
        return _resolve_ref(ctx.gate, "gate", ref_name)
    raise TemplateError(f"unknown reference {{{ref_type}:{ref_name}}}")


def _resolve_ref(resolver: Callable[[str], str] | None, kind: str, name: str | None) -> str:
    if resolver is None:
        raise TemplateError(
            f"{{{kind}:{name}}} cannot be resolved here (no {kind} resolver — plan time?)"
        )
    try:
        value = resolver(name)  # type: ignore[arg-type]
    except (KeyError, LookupError) as e:
        raise TemplateError(f"unknown {kind} {{{kind}:{name}}}") from e
    if value is None:
        raise TemplateError(f"unknown {kind} {{{kind}:{name}}}")
    return str(value)


def _render_helper(name: str, argstr: str, ctx: TemplateContext, body: str) -> str:
    if name == "slug":
        return _slugify(_render_value(argstr.strip(), ctx))
    if name == "dash":
        value = _render_value(argstr.strip(), ctx)
        return f"-{value}" if value else ""
    if name == "short":
        parts = [a.strip() for a in argstr.split(",")]
        if len(parts) != 2:
            raise TemplateError(f"short(value, n) takes exactly two args: {{{body}}}")
        value = _render_value(parts[0], ctx)
        try:
            n = int(parts[1])
        except ValueError as e:
            raise TemplateError(f"short(...) length must be an integer: {{{body}}}") from e
        return value[:n]
    raise TemplateError(f"unknown helper {{{body}}}")  # pragma: no cover - guarded by HELPERS


def _lookup(mapping: Mapping[str, Any], key: str, body: str) -> str:
    if key not in mapping:
        raise TemplateError(f"missing value for {{{body}}}")
    return str(mapping[key])


def _clock(ctx: TemplateContext) -> datetime:
    if ctx.now is None:
        raise TemplateError("{date}/{datetime} need an injected clock (ctx.now is None)")
    return ctx.now


def _slugify(value: str) -> str:
    """Hostname → kebab-case, ``www.`` and the TLD stripped (docs §2.8)."""
    from urllib.parse import urlparse

    raw = str(value).strip()
    parsed = urlparse(raw if "://" in raw else "http://" + raw)
    host = parsed.hostname or raw  # .hostname drops any :port
    if host.startswith("www."):
        host = host[len("www."):]
    if "." in host:
        host = host.rsplit(".", 1)[0]  # drop the TLD segment
    return re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")


# --- scan --------------------------------------------------------------------

def scan(text: str) -> list[Placeholder]:
    """Enumerate the placeholders in ``text`` without resolving them."""
    return [_classify(m.group(1).strip()) for m in _PLACEHOLDER.finditer(text)]


def _classify(body: str) -> Placeholder:
    helper = _HELPER_CALL.fullmatch(body)
    if helper and helper.group(1) in HELPERS:
        return Placeholder(kind="helper", raw=body)
    if body == "run_dir":
        return Placeholder(kind="reference", raw=body, ref_type="run_dir")
    if ":" in body:
        ref_type, _, ref_name = body.partition(":")
        return Placeholder(
            kind="reference", raw=body, ref_type=ref_type.strip(), ref_name=ref_name.strip()
        )
    return Placeholder(kind="value", raw=body)
