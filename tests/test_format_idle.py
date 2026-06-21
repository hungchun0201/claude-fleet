"""Tests for patrol._format_idle, especially the >24h day+hour formatting."""
from __future__ import annotations

import pytest

from core.patrol import _format_idle

HOUR = 3600
DAY = 86400


@pytest.mark.unit
@pytest.mark.parametrize(
    "seconds,expected",
    [
        (5, "5s"),
        (59, "59s"),
        (60, "1m"),
        (90, "1m"),
        (HOUR, "1h"),
        (13 * HOUR + 41 * 60, "13h41m"),
        (23 * HOUR + 59 * 60, "23h59m"),
        # Boundary: exactly one day rolls over to day granularity.
        (DAY, "1d"),
        (DAY + 3 * HOUR, "1d 3h"),
        (2 * DAY + 3 * HOUR, "2d 3h"),
        # Hours, not minutes, past a day — and drop the hour when it's zero.
        (51 * HOUR, "2d 3h"),
        (7 * DAY, "7d"),
    ],
)
def test_format_idle(seconds: int, expected: str) -> None:
    assert _format_idle(seconds) == expected
