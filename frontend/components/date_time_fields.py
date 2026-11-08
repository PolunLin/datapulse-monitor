"""Date picker and hour/minute/second dropdowns in Taiwan standard time."""

from datetime import date, datetime, time

import streamlit as st

from utils.timezone import TAIPEI


def date_time_fields(
    prefix: str, label: str, default: datetime | None = None, *, required: bool = False
) -> str:
    selected_date = st.date_input(
        f"{label}日期（台灣時間）",
        value=default.date() if default else None,
        min_value=date(1000, 1, 1),
        max_value=date(9999, 12, 31),
        key=f"{prefix}_date",
        persist_state="session",
        required=required,
    )
    st.caption(f"{label}時間（台灣時間）")
    parts = []
    for column, name, suffix, limit in zip(
        st.columns(3),
        ["小時", "分鐘", "秒數"],
        ["hour", "minute", "second"],
        [24, 60, 60],
        strict=True,
    ):
        parts.append(
            column.selectbox(
                f"{label}{name}",
                range(limit),
                index=getattr(default, suffix) if default else None,
                format_func=lambda value: f"{value:02d}",
                placeholder=f"選擇{name}",
                key=f"{prefix}_{suffix}",
                required=required,
                persist_state="session",
            )
        )
    if selected_date is None:
        return "未選擇日期" if required else ""
    if any(part is None for part in parts):
        # Preserve an invalid value for submit-time validation instead of
        # silently dropping the date filter or crashing the render.
        return "未選擇時間"
    selected_time = time(*parts)
    # The picker displays seconds; preserve existing fractional seconds when the
    # user edits other fields without changing this timestamp.
    if (
        default
        and selected_date == default.date()
        and selected_time == default.time().replace(microsecond=0)
    ):
        return default.isoformat()
    return datetime.combine(selected_date, selected_time, tzinfo=TAIPEI).isoformat()


def time_range_fields(prefix: str) -> tuple[str, str]:
    st.caption("以台灣時間查詢：開始包含、結束不包含；日期留空表示不限。")
    start, end = st.columns(2)
    with start:
        start_value = date_time_fields(f"{prefix}_start", "開始")
    with end:
        end_value = date_time_fields(f"{prefix}_end", "結束")
    return start_value, end_value
