"""The cron parsing engine: normalize a 5-field expression (or @macro) → CronSpec.

Behaviour tests against parse_cron — the shared engine behind every backend renderer.
"""

from __future__ import annotations

import pytest

from cairn.kernel.schedkit import parse_cron


def test_all_wildcards():
    spec = parse_cron("* * * * *")
    assert spec.minute.wildcard and spec.minute.values == ()
    assert spec.dow.wildcard


def test_single_values():
    spec = parse_cron("30 2 * * *")
    assert not spec.minute.wildcard
    assert spec.minute.values == (30,)
    assert spec.hour.values == (2,)
    assert spec.dom.wildcard


def test_list_and_range_and_step():
    spec = parse_cron("0,15,30,45 9-17 * * *")
    assert spec.minute.values == (0, 15, 30, 45)
    assert spec.hour.values == tuple(range(9, 18))


def test_step_over_wildcard_is_explicit_not_wildcard():
    spec = parse_cron("*/15 * * * *")
    assert not spec.minute.wildcard
    assert spec.minute.values == (0, 15, 30, 45)


def test_dow_seven_normalizes_to_zero():
    assert parse_cron("0 0 * * 7").dow.values == (0,)
    assert parse_cron("0 0 * * 0").dow.values == (0,)


def test_month_and_dow_names():
    spec = parse_cron("0 0 1 jan mon")
    assert spec.month.values == (1,)
    assert spec.dow.values == (1,)


def test_macros_normalize():
    assert parse_cron("@daily") == parse_cron("0 0 * * *")
    assert parse_cron("@weekly") == parse_cron("0 0 * * 0")
    assert parse_cron("@hourly") == parse_cron("0 * * * *")


def test_reboot_rejected():
    with pytest.raises(ValueError, match="@reboot"):
        parse_cron("@reboot")


@pytest.mark.parametrize(
    "expr",
    [
        "* * * *",          # too few fields
        "* * * * * *",      # too many fields
        "60 * * * *",       # minute out of range
        "* 24 * * *",       # hour out of range
        "* * 0 * *",        # dom below 1
        "* * * 13 *",       # month out of range
        "* * * * 8",        # dow out of range
        "5-2 * * * *",      # descending range
        "*/0 * * * *",      # zero step
        "foo * * * *",      # garbage token
    ],
)
def test_invalid_expressions_raise(expr):
    with pytest.raises(ValueError):
        parse_cron(expr)
