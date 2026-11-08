"""Taiwan presentation time; API values and stored instants remain timezone-aware."""

from datetime import UTC, datetime, timedelta, timezone

DISPLAY_TIMEZONE = "UTC+08:00"
# The product uses present-day Taiwan standard time for every supported date,
# matching database aggregation; historical DST/LMT rules are not applied.
TAIPEI = timezone(timedelta(hours=8), name=DISPLAY_TIMEZONE)
TIME_LABEL = "台灣時間"


def as_taipei(value: datetime | str) -> datetime:
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return instant.astimezone(TAIPEI)


def format_taipei(value: datetime | str, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    try:
        return as_taipei(value).strftime(fmt)
    except OverflowError:
        return _overflow_label(value)


def taipei_iso(value: datetime | str) -> str:
    try:
        return as_taipei(value).isoformat()
    except OverflowError:
        return _overflow_label(value)


def _overflow_label(value: datetime | str) -> str:
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    original = instant.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return f"{original}（UTC；超出台灣時間可顯示範圍）"


def can_display_taipei(value: datetime | str) -> bool:
    try:
        as_taipei(value)
    except OverflowError:
        return False
    return True


def chart_time(value: datetime | str) -> str:
    """Encode Taipei wall time on a UTC chart scale, independent of browser timezone.

    This synthetic timestamp is only a plotting coordinate, never an API/storage
    instant. An explicit Z avoids browser-local parsing of offsetless ISO strings.
    """
    return as_taipei(value).replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
