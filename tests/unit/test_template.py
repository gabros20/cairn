"""The template mini-language (docs/API.md §2.8): render() and scan().

Tests exercise the public surface — render(text, ctx), scan(text),
TemplateContext — with an injected clock (never the real wall clock) so date
formatting is deterministic.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from cairn.kernel.errors import CairnError
from cairn.kernel.template import (
    Placeholder,
    TemplateContext,
    TemplateError,
    render,
    scan,
)

NOW = datetime(2026, 7, 3, 9, 5)  # -> 20260703 / 20260703-0905


def ctx(**kw) -> TemplateContext:
    base = dict(
        params={"mode": "redesign", "url": "https://www.mcmparchitects.com/", "variant": ""},
        dims={"design": "reproduce"},
        pipeline="brease-rebuild",
        now=NOW,
    )
    base.update(kw)
    return TemplateContext(**base)


# ---- value placeholders -----------------------------------------------------

def test_params_and_dims_values():
    assert render("{params.mode}", ctx()) == "redesign"
    assert render("{dims.design}", ctx()) == "reproduce"


def test_pipeline_and_dates_use_injected_clock():
    assert render("{pipeline}", ctx()) == "brease-rebuild"
    assert render("{date}", ctx()) == "20260703"
    assert render("{datetime}", ctx()) == "20260703-0905"


def test_render_is_deterministic_for_a_fixed_now():
    assert render("{datetime}", ctx()) == render("{datetime}", ctx())


def test_non_string_value_is_stringified():
    assert render("{params.n}", ctx(params={"n": 7})) == "7"


def test_mixed_literal_and_placeholders():
    out = render("run-{params.mode}-{date}", ctx())
    assert out == "run-redesign-20260703"


# ---- missing values are errors, never silently empty ------------------------

def test_missing_param_raises_naming_the_placeholder():
    with pytest.raises(TemplateError) as exc:
        render("{params.nope}", ctx())
    assert "nope" in str(exc.value)


def test_unknown_placeholder_raises():
    with pytest.raises(TemplateError):
        render("{bogus}", ctx())


def test_template_error_is_a_cairn_error():
    assert issubclass(TemplateError, CairnError)


# ---- cycle ------------------------------------------------------------------

def test_cycle_renders_when_bound():
    assert render("c{cycle}", ctx(cycle=2)) == "c2"


def test_cycle_unbound_raises():
    with pytest.raises(TemplateError):
        render("{cycle}", ctx())  # cycle defaults to None


# ---- date/datetime need a clock ---------------------------------------------

def test_date_without_clock_raises():
    with pytest.raises(TemplateError):
        render("{date}", ctx(now=None))


# ---- helpers: slug ----------------------------------------------------------

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.mcmparchitects.com/", "mcmparchitects"),   # www + TLD + trailing slash
        ("https://mcmparchitects.com", "mcmparchitects"),        # non-www
        ("http://acme.com/team/", "acme"),                       # path ignored
        ("https://blog.acme.co", "blog-acme"),                   # subdomain kept, TLD stripped
        ("http://localhost:3000", "localhost"),                  # port stripped, no TLD
        ("acme.com", "acme"),                                    # no scheme
    ],
)
def test_slug_edge_cases(url, expected):
    assert render("{slug(params.url)}", ctx(params={"url": url})) == expected


# ---- helpers: dash ----------------------------------------------------------

def test_dash_non_empty_prefixes_a_hyphen():
    assert render("{dash(params.variant)}", ctx(params={"variant": "blue"})) == "-blue"


def test_dash_empty_is_empty_string():
    assert render("{dash(params.variant)}", ctx(params={"variant": ""})) == ""


# ---- helpers: short ---------------------------------------------------------

def test_short_truncates_to_n_chars():
    assert render("{short(params.mode, 3)}", ctx(params={"mode": "redesign"})) == "red"


# ---- a realistic run_id template --------------------------------------------

def test_run_id_composition():
    out = render("{slug(params.url)}-{params.mode}-{date}{dash(params.variant)}", ctx())
    assert out == "mcmparchitects-redesign-20260703"
    out2 = render(
        "{slug(params.url)}-{params.mode}-{date}{dash(params.variant)}",
        ctx(params={"url": "https://acme.com", "mode": "reimagine", "variant": "b"}),
    )
    assert out2 == "acme-reimagine-20260703-b"


# ---- reference placeholders -------------------------------------------------

def test_run_dir_reference():
    assert render("{run_dir}/captures", ctx(run_dir="/runs/x")) == "/runs/x/captures"


def test_run_dir_missing_raises():
    with pytest.raises(TemplateError):
        render("{run_dir}", ctx())  # run_dir defaults to None


def test_artifact_reference_uses_resolver():
    out = render("{artifact:hero}", ctx(artifact=lambda name: f"/runs/x/{name}.json"))
    assert out == "/runs/x/hero.json"


def test_gate_reference_uses_resolver():
    out = render("{gate:scope}", ctx(gate=lambda name: "all"))
    assert out == "all"


def test_artifact_reference_without_resolver_raises():
    with pytest.raises(TemplateError):
        render("{artifact:hero}", ctx())  # no resolver -> plan-time, cannot resolve


def test_artifact_reference_illegal_in_path_mode():
    # {artifact:…} inside an artifact.path template is structurally illegal
    c = ctx(artifact=lambda name: "/x", artifact_refs_allowed=False)
    with pytest.raises(TemplateError):
        render("{artifact:hero}/out", c)


# ---- scan (plan-time introspection) -----------------------------------------

def test_scan_classifies_each_placeholder_kind():
    found = scan("{params.mode}/{artifact:hero}-{slug(params.url)}-{run_dir}")
    kinds = [(p.kind, p.raw) for p in found]
    assert ("value", "params.mode") in kinds
    assert ("reference", "artifact:hero") in kinds
    assert ("helper", "slug(params.url)") in kinds
    assert ("reference", "run_dir") in kinds


def test_scan_exposes_reference_target():
    (p,) = scan("{artifact:hero}")
    assert isinstance(p, Placeholder)
    assert p.kind == "reference"
    assert p.ref_type == "artifact"
    assert p.ref_name == "hero"


def test_scan_of_plain_text_is_empty():
    assert scan("no placeholders here") == []
