"""Taiwan display conversion must preserve instants and ignore host/browser timezone."""

from datetime import UTC, datetime, timedelta

import pytest

from utils.timezone import (
    TAIPEI,
    as_taipei,
    can_display_taipei,
    chart_time,
    format_taipei,
    taipei_iso,
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("2026-11-08T15:59:59Z", "2026-11-08T23:59:59+08:00"),
        ("2026-11-08T16:00:00Z", "2026-11-09T00:00:00+08:00"),
        ("2026-12-31T16:00:00Z", "2027-01-01T00:00:00+08:00"),
        ("2026-11-08T12:34:56.123456+08:00", "2026-11-08T12:34:56.123456+08:00"),
        ("2026-11-08T00:00:00-08:00", "2026-11-08T16:00:00+08:00"),
        ("1945-07-01T16:00:00Z", "1945-07-02T00:00:00+08:00"),
        ("1000-01-01T00:00:00Z", "1000-01-01T08:00:00+08:00"),
    ],
)
def test_taiwan_conversion_preserves_instant_and_rolls_over_date(source, expected):
    actual = as_taipei(source)
    assert actual.tzinfo == TAIPEI
    assert actual == datetime.fromisoformat(source)
    assert taipei_iso(source) == expected
    assert format_taipei(source) == actual.strftime("%Y-%m-%d %H:%M:%S")


def test_chart_coordinate_has_explicit_zone_and_is_presentation_only():
    instant = datetime(2026, 11, 8, 16, 0, 0, 123456, tzinfo=UTC)
    coordinate = chart_time(instant)
    assert coordinate == "2026-11-09T00:00:00.123456Z"
    assert datetime.fromisoformat(coordinate) - instant == timedelta(hours=8)
    assert instant.hour == 16 and instant.tzinfo == UTC


@pytest.mark.parametrize("value", ["2026-11-08T12:00:00", datetime(2026, 11, 8, 12)])
def test_conversion_rejects_missing_timezone_instead_of_using_host_timezone(value):
    with pytest.raises(ValueError, match="timezone-aware"):
        as_taipei(value)


def test_display_boundary_preserves_original_utc_instead_of_crashing_or_mislabeling():
    instant = datetime.max.replace(tzinfo=UTC)
    assert not can_display_taipei(instant)
    assert taipei_iso(instant) == "9999-12-31T23:59:59.999999Z（UTC；超出台灣時間可顯示範圍）"
    assert format_taipei(instant) == taipei_iso(instant)
    with pytest.raises(OverflowError):
        chart_time(instant)
    assert can_display_taipei("9999-12-31T15:59:59Z")
