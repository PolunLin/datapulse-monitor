"""Aggregate charts keep exact decimal text in their tooltips."""

from datetime import datetime, timedelta

import altair as alt
import pandas as pd

from components.charts import TEAL
from utils.devices import category_label
from utils.timezone import can_display_taipei, chart_time, format_taipei


def category_chart(rows: list[dict], *, system: bool = False) -> alt.Chart:
    top = sorted(rows, key=lambda row: (-row["count"], row["category"]))[:20]
    frame = pd.DataFrame(
        [
            {
                "category": row["category"],
                "category_label": "舊資料（未指定設備）"
                if system and row["category"] == "legacy"
                else category_label(row["category"]),
                "average": float(row["average"]),
                "average_exact": row["average"],
                "count": row["count"],
            }
            for row in top
        ]
    )
    return (
        alt.Chart(frame)
        .mark_bar(color=TEAL)
        .encode(
            x=alt.X("average:Q", title="平均值"),
            y=alt.Y(
                "category_label:N",
                title="設備／分類",
                sort=alt.SortField(field="count", order="descending"),
            ),
            tooltip=[
                alt.Tooltip("category_label:N", title="設備／分類"),
                alt.Tooltip("average_exact:N", title="平均值"),
                alt.Tooltip("count:Q", title="筆數"),
            ],
        )
        .properties(height=max(180, min(600, len(top) * 28)))
        .configure_view(stroke=None)
    )


def aggregate_trend(rows: list[dict], interval: str) -> alt.Chart:
    expected = timedelta(hours=1) if interval == "hour" else timedelta(days=1)
    segment = 0
    previous = None
    points = []
    for row in rows:
        if not can_display_taipei(row["timestamp"]):
            continue
        instant = datetime.fromisoformat(row["timestamp"])
        if previous is not None and instant - previous != expected:
            segment += 1
        previous = instant
        points.append(
            {
                "timestamp": chart_time(instant),
                "timestamp_utc": instant.isoformat(),
                "timestamp_label": format_taipei(instant, "%Y-%m-%d %H:%M 台灣時間"),
                "average": float(row["average"]),
                "average_exact": row["average"],
                "count": row["count"],
                "segment": segment,
            }
        )
    return (
        alt.Chart(
            pd.DataFrame(
                points,
                columns=[
                    "timestamp",
                    "timestamp_utc",
                    "timestamp_label",
                    "average",
                    "average_exact",
                    "count",
                    "segment",
                ],
            )
        )
        .mark_line(color=TEAL, point=True)
        .encode(
            # chart_time shifts only chart coordinates, keeping Taiwan wall-clock
            # labels stable even when the browser uses a different timezone.
            x=alt.X("timestamp:T", title="時間（台灣時間）", scale=alt.Scale(type="utc")),
            y=alt.Y("average:Q", title="平均值", scale=alt.Scale(zero=False)),
            detail="segment:N",
            tooltip=[
                alt.Tooltip("timestamp_label:N", title="區間開始"),
                alt.Tooltip("average_exact:N", title="平均值"),
                alt.Tooltip("count:Q", title="筆數"),
            ],
        )
        .properties(height=260)
        .configure_view(stroke=None)
    )
