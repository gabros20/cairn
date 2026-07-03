"""Duration parser: '30m'/'45s'/'2h' (and compounds) → seconds; garbage raises."""

from __future__ import annotations

import pytest

from cairn.kernel.config import parse_duration


@pytest.mark.parametrize(
    "text,seconds",
    [
        ("45s", 45),
        ("30m", 1800),
        ("2h", 7200),
        ("1h30m", 5400),
        ("90", 90),  # bare integer = seconds
    ],
)
def test_parse_duration_happy(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("garbage", ["", "abc", "30x", "m", "1.5h", "-5s", "30 m"])
def test_parse_duration_rejects_garbage(garbage):
    with pytest.raises(ValueError):
        parse_duration(garbage)
