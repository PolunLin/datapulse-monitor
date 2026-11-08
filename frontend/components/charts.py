"""Plot values as floats only at the chart boundary; retain exact strings in tooltips."""

import altair as alt
import pandas as pd

from clients.realtime import SampleEvent
from utils.timezone import as_taipei, can_display_taipei, chart_time, format_taipei

TEAL = "#00897B"
RED = "#DC3545"


def event_frame(events: list[SampleEvent]) -> pd.DataFrame:
    events = sorted(
        (event for event in events if can_display_taipei(event.timestamp)),
        key=lambda event: (event.timestamp, event.event_id),
    )
    # A long pause is missing data, so don't draw a fictitious trend across it.
    previous = {}
    segments = []
    for event in events:
        series = (event.device_id, event.category, event.unit)
        last, segment = previous.get(series, (event.timestamp, 0))
        gap = (event.timestamp - last).total_seconds()
        if gap > 10 or gap < 0:
            segment += 1
        previous[series] = (event.timestamp, segment)
        segments.append(segment)
    return pd.DataFrame(
        [
            {
                # UTC scale uses a presentation coordinate; preserve the actual instant separately.
                "timestamp": chart_time(event.timestamp),
                "timestamp_utc": event.timestamp.isoformat(),
                "time_label": format_taipei(event.timestamp, "%H:%M:%S"),
                "timestamp_label": format_taipei(event.timestamp) + " 台灣時間",
                "category": event.category,
                "device_id": event.device_id,
                "device_name": event.device_name,
                "unit": event.unit,
                "value": float(event.value),
                "value_exact": str(event.value),
                "threshold": float(event.threshold),
                "threshold_exact": str(event.threshold),
                "is_anomaly": event.is_anomaly,
                "event_id": event.event_id,
                "segment": segment,
            }
            for event, segment in zip(events, segments, strict=True)
        ]
    )


def value_label(events: list[SampleEvent]) -> str:
    units = {event.unit for event in events}
    if len(units) == 1 and (unit := next(iter(units))):
        return f"數值（{unit}）"
    return "數值"


def trend_chart(events: list[SampleEvent]) -> alt.LayerChart:
    events = [event for event in events if can_display_taipei(event.timestamp)]
    show_dates = len({as_taipei(event.timestamp).date() for event in events}) > 1
    base = alt.Chart(event_frame(events)).encode(
        x=alt.X(
            "timestamp:T",
            title="時間（台灣）",
            scale=alt.Scale(type="utc"),
            axis=alt.Axis(format="%m/%d %H:%M" if show_dates else "%H:%M:%S"),
        ),
        tooltip=[
            alt.Tooltip("timestamp_label:N", title="時間（台灣）"),
            alt.Tooltip("device_name:N", title="設備"),
            alt.Tooltip("unit:N", title="單位"),
            alt.Tooltip("category:N", title="分類"),
            alt.Tooltip("value_exact:N", title="數值"),
            alt.Tooltip("threshold_exact:N", title="閾值"),
        ],
        detail=["device_id:N", "category:N", "unit:N", "segment:N"],
    )
    line = base.mark_line(
        color=TEAL, strokeWidth=2, point=alt.OverlayMarkDef(color=TEAL, size=18, filled=True)
    ).encode(y=alt.Y("value:Q", title=value_label(events), scale=alt.Scale(zero=True)))
    threshold = base.mark_line(color="#8994A3", strokeDash=[6, 4]).encode(
        y=alt.Y("threshold:Q", title=value_label(events))
    )
    anomalies = (
        base.transform_filter(alt.datum.is_anomaly)
        .mark_point(color=RED, size=65, filled=True)
        .encode(y="value:Q")
    )
    layers = line + threshold
    if any(event.is_anomaly for event in events):
        layers = layers + anomalies
    return layers.properties(height=220).configure_view(stroke=None)


def bar_chart(events: list[SampleEvent]) -> alt.Chart:
    events = [event for event in events if can_display_taipei(event.timestamp)]
    return (
        alt.Chart(event_frame(events[-30:]))
        .mark_bar()
        .encode(
            x=alt.X(
                "timestamp:T",
                title="時間（台灣）",
                scale=alt.Scale(type="utc"),
                axis=alt.Axis(format="%H:%M:%S"),
            ),
            y=alt.Y("value:Q", title=value_label(events)),
            color=alt.condition(alt.datum.is_anomaly, alt.value(RED), alt.value(TEAL)),
            tooltip=[
                alt.Tooltip("timestamp_label:N", title="時間（台灣）"),
                alt.Tooltip("device_name:N", title="設備"),
                alt.Tooltip("unit:N", title="單位"),
                alt.Tooltip("category:N", title="分類"),
                alt.Tooltip("value_exact:N", title="數值"),
                alt.Tooltip("threshold_exact:N", title="閾值"),
            ],
        )
        .properties(height=170)
        .configure_view(stroke=None)
    )
