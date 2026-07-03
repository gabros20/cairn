"""The expression language (docs/API.md §5): parsing, evaluation, roots.

Tests exercise the public surface only — parse(), Expr.evaluate(resolver),
Expr.roots() — and the two error types. A resolver is a plain callable, so the
tests supply a dict-backed one; expr.py never touches the filesystem.
"""

from __future__ import annotations

import pytest

from cairn.kernel.errors import CairnError
from cairn.kernel.expr import EvalError, Expr, ExprError, parse


def make_resolver(data, *, missing_raises=True):
    """resolver(root, parts) walking nested dicts; missing root/part -> KeyError."""

    def resolve(root, parts):
        if root not in data:
            raise KeyError(root)
        cur = data[root]
        for p in parts:
            cur = cur[p]  # KeyError on a missing key
        return cur

    return resolve


DATA = {
    "params": {"pages": "gate", "mode": "redesign", "flag": True, "variant": ""},
    "dims": {"design": "reproduce", "brand": "keep"},
    "artifacts": {"art-review": {"verdict": "approve"}, "list": ["a", "b"]},
    "gates": {"scope": "all"},
    "run": {"count": 3},
    "cycle": 2,
}
R = make_resolver(DATA)


# ---- literals & basic evaluation --------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("true", True),
        ("false", False),
        ("'hello'", "hello"),
        ("42", 42),
        ("3.5", 3.5),
        ("-7", -7),
    ],
)
def test_literals_evaluate(text, expected):
    assert parse(text).evaluate(R) == expected


def test_bare_path_returns_its_value():
    assert parse("params.mode").evaluate(R) == "redesign"
    assert parse("cycle").evaluate(R) == 2
    assert parse("run.count").evaluate(R) == 3


def test_hyphenated_artifact_segment_walks_json():
    # artifact names are kebab-case; path idents must accept hyphens
    assert parse("artifacts.art-review.verdict").evaluate(R) == "approve"


# ---- comparison & membership ------------------------------------------------

def test_equality_and_inequality():
    assert parse("params.pages == 'gate'").evaluate(R) is True
    assert parse("params.pages == 'all'").evaluate(R) is False
    assert parse("dims.design != 'reproduce'").evaluate(R) is False
    assert parse("run.count == 3").evaluate(R) is True


def test_membership():
    assert parse("'a' in artifacts.list").evaluate(R) is True
    assert parse("'z' in artifacts.list").evaluate(R) is False


def test_membership_on_non_iterable_raises_eval_error():
    with pytest.raises(EvalError):
        parse("'x' in run.count").evaluate(R)


# ---- negation ---------------------------------------------------------------

def test_not_negates_truthiness():
    assert parse("!params.flag").evaluate(R) is False
    assert parse("!(params.pages == 'all')").evaluate(R) is True


# ---- boolean ops, precedence, short-circuit ---------------------------------

def test_and_or_return_booleans():
    assert parse("true && false").evaluate(R) is False
    assert parse("true || false").evaluate(R) is True


def test_and_binds_tighter_than_or():
    # true && false || true  ==  (true && false) || true  == True
    assert parse("true && false || true").evaluate(R) is True
    # false || true && false  ==  false || (true && false)  == False
    assert parse("false || true && false").evaluate(R) is False


def test_parentheses_override_precedence():
    assert parse("(true || false) && false").evaluate(R) is False


def test_short_circuit_and_does_not_evaluate_rhs():
    # rhs references a missing path; && must not touch it once lhs is false
    assert parse("false && artifacts.nope.x").evaluate(R) is False


def test_short_circuit_or_does_not_evaluate_rhs():
    assert parse("true || artifacts.nope.x").evaluate(R) is True


# ---- missing paths are errors, never falsy ----------------------------------

def test_missing_path_raises_eval_error_not_falsy():
    with pytest.raises(EvalError):
        parse("artifacts.nope.verdict").evaluate(R)


def test_missing_root_raises_eval_error():
    with pytest.raises(EvalError):
        parse("params.typo == 'x'").evaluate(R)


def test_eval_error_is_a_cairn_error():
    assert issubclass(EvalError, CairnError)
    assert issubclass(ExprError, CairnError)


# ---- roots() ----------------------------------------------------------------

def test_roots_reports_touched_root_names():
    e = parse("params.pages == 'gate' && artifacts.art-review.verdict == 'approve'")
    assert e.roots() == {"params", "artifacts"}


def test_roots_of_pure_literal_is_empty():
    assert parse("true").roots() == set()


def test_roots_includes_cycle_and_gates():
    assert parse("cycle == 1 || gates.scope == 'all'").roots() == {"cycle", "gates"}


# ---- syntax errors carry a position -----------------------------------------

@pytest.mark.parametrize(
    "bad",
    [
        "params.",            # dangling dot
        "params.x ==",        # missing rhs
        "&& true",            # leading operator
        "'unterminated",      # unterminated string
        "foo",                # unknown identifier (not a root)
        "(true && false",     # unclosed paren
        "true false",         # trailing input, no operator
        "",                   # empty
        "params.x = 'y'",     # single '=' is not valid
    ],
)
def test_syntax_errors_raise_expr_error(bad):
    with pytest.raises(ExprError):
        parse(bad)


def test_expr_error_exposes_position():
    with pytest.raises(ExprError) as exc:
        parse("true &&")
    assert isinstance(exc.value.position, int)
    assert exc.value.position >= 0
