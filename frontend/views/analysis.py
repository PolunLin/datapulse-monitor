"""One source-scoped home for personal records and equipment monitoring history."""

from datetime import datetime
from uuid import UUID

import streamlit as st

from clients.api import XLSX_MIME, APIError
from components.analysis_charts import aggregate_trend, category_chart
from components.analysis_history import render as render_history
from components.date_time_fields import time_range_fields
from state import session
from state.analysis import MINE, SOURCES, SYSTEM, clear_results
from utils.devices import DEVICE_CATEGORIES, category_label
from utils.timezone import TAIPEI, can_display_taipei, format_taipei, taipei_iso
from views.records import _filter_time

INTERVALS = {"每日": "day", "每小時": "hour"}
ANOMALIES = {"全部": None, "僅異常": True, "正常": False}
LEGACY = "legacy"


def _device_label(value: str, system: bool) -> str:
    if not value:
        return "全部設備"
    if value == LEGACY:
        return "舊資料（未指定設備）" if system else "指定舊分類"
    return DEVICE_CATEGORIES[value]


def _scoped_filters(user_id: int, previous: dict) -> dict:
    source = SOURCES[st.session_state.get("analysis_filter_source", MINE)]
    device = st.session_state.get("analysis_filter_category", "")
    result = {
        key: value
        for key, value in previous.items()
        if key in {"interval", "q", "start_time", "end_time"}
    }
    result.update(source=source, interval=result.get("interval", "day"))
    if source == "records":
        result["created_by"] = user_id
        if device == LEGACY:
            old = st.session_state.get("analysis_filter_legacy", "").strip()
            if not old:
                raise ValueError("請填寫要查詢的舊分類名稱，再按「查詢分析」。")
            result["category"] = old
        elif device:
            result["category"] = device
    else:
        if device:
            result["device_id"] = device
        if previous.get("source") == "realtime":
            result.update(
                {key: previous[key] for key in ("event_id", "is_anomaly") if key in previous}
            )
    return result


def _scope_changed() -> None:
    clear_results()
    st.session_state["analysis_history_page"] = 1
    st.session_state["analysis_filter_invalid"] = False
    try:
        st.session_state["analysis_filters"] = _scoped_filters(
            st.session_state["analysis_owner_id"], st.session_state.get("analysis_filters", {})
        )
        st.session_state.pop("analysis_scope_error", None)
    except ValueError as exc:
        st.session_state["analysis_scope_error"] = str(exc)
    if st.session_state.get("analysis_filter_source") == MINE:
        st.session_state.pop("analysis_filter_event", None)
        st.session_state.pop("analysis_filter_anomaly", None)


def _initialize(user_id: int) -> None:
    previous_owner = st.session_state.get("analysis_owner_id")
    if previous_owner is not None and previous_owner != user_id:
        for key in list(st.session_state):
            if key.startswith("analysis_"):
                del st.session_state[key]
    if previous_owner != user_id:
        clear_results()
        st.session_state["analysis_owner_id"] = user_id
    filters = st.session_state.get("analysis_filters", {"source": "records", "interval": "day"})
    source = SYSTEM if filters.get("source") == "realtime" else MINE
    if st.session_state.get("analysis_filter_source") not in SOURCES:
        st.session_state["analysis_filter_source"] = source
    selected = filters.get("device_id" if source == SYSTEM else "category", "")
    if "analysis_filter_category" not in st.session_state:
        st.session_state["analysis_filter_category"] = (
            selected if selected in {"", *DEVICE_CATEGORIES, LEGACY} else LEGACY
        )
        if selected and selected not in DEVICE_CATEGORIES and source == MINE:
            st.session_state["analysis_filter_legacy"] = selected
    if st.session_state["analysis_filter_category"] not in {"", *DEVICE_CATEGORIES, LEGACY}:
        st.session_state["analysis_filter_category"] = ""
        clear_results()
    try:
        scoped = _scoped_filters(user_id, filters)
        if scoped != filters:
            clear_results()
        st.session_state["analysis_filters"] = scoped
        st.session_state.pop("analysis_scope_error", None)
    except ValueError as exc:
        st.session_state["analysis_scope_error"] = str(exc)


def _filters(user_id: int) -> tuple[dict, bool]:
    _initialize(user_id)
    source_col, category_col = st.columns(2)
    source_label = source_col.selectbox(
        "資料來源",
        list(SOURCES),
        key="analysis_filter_source",
        on_change=_scope_changed,
        persist_state="session",
    )
    system = source_label == SYSTEM
    device = category_col.selectbox(
        "分類（設備）",
        ["", *DEVICE_CATEGORIES, LEGACY],
        format_func=lambda value: _device_label(value, system),
        key="analysis_filter_category",
        on_change=_scope_changed,
        persist_state="session",
    )
    st.caption(
        "我的資料包含目前登入者手動建立及 CSV／JSON 匯入的記錄；"
        "系統資料為模擬設備持續產生的監控歷史。"
    )
    with st.form("analysis_filter_form"):
        q = st.text_input(
            "搜尋標題", max_chars=200, key="analysis_filter_q", persist_state="session"
        )
        legacy = ""
        if not system and device == LEGACY:
            legacy = st.text_input(
                "舊分類名稱",
                max_chars=100,
                key="analysis_filter_legacy",
                help="只查詢既有舊分類，不會將其冒充為設備 A／B／C。",
                persist_state="session",
            )
        start, end = time_range_fields("analysis_filter")
        interval = st.selectbox(
            "趨勢分組", list(INTERVALS), key="analysis_filter_interval", persist_state="session"
        )
        anomaly, event = "全部", ""
        if system:
            anomaly = st.selectbox(
                "異常狀態", list(ANOMALIES), key="analysis_filter_anomaly", persist_state="session"
            )
            with st.expander("進階查詢"):
                event = st.text_input(
                    "事件 ID（選填）",
                    key="analysis_filter_event",
                    max_chars=36,
                    help="輸入完整事件 UUID，查詢同一筆已保存的監測事件。",
                    persist_state="session",
                ).strip()
        submitted = st.form_submit_button("查詢分析", type="primary")
    if submitted:
        clear_results()
        st.session_state["analysis_history_page"] = 1
        try:
            start, end = _filter_time(start), _filter_time(end)
            if start and end and datetime.fromisoformat(start) >= datetime.fromisoformat(end):
                raise ValueError("開始時間必須早於結束時間。")
            if not system and device == LEGACY and not legacy.strip():
                raise ValueError("請填寫要查詢的舊分類名稱。")
            if event:
                try:
                    event = str(UUID(event))
                except ValueError as exc:
                    raise ValueError("事件 ID 必須為有效的 UUID。") from exc
            applied = {
                "source": SOURCES[source_label],
                "interval": INTERVALS[interval],
                **({"q": q.strip()} if q.strip() else {}),
                **({"start_time": start} if start else {}),
                **({"end_time": end} if end else {}),
                **({"is_anomaly": ANOMALIES[anomaly]} if ANOMALIES[anomaly] is not None else {}),
                **({"event_id": event} if event else {}),
            }
            st.session_state["analysis_filters"] = _scoped_filters(user_id, applied)
        except ValueError as exc:
            st.session_state["analysis_filter_invalid"] = True
            st.error(str(exc))
            return {}, False
        st.session_state["analysis_filter_invalid"] = False
        st.session_state.pop("analysis_scope_error", None)
    if error := st.session_state.get("analysis_scope_error"):
        st.info(error)
        return {}, False
    return st.session_state["analysis_filters"], not st.session_state.get(
        "analysis_filter_invalid", False
    )


def _display_result(result: dict) -> None:
    summary = result["summary"]
    labels = {
        "count": "資料筆數",
        "total": "總計",
        "average": "平均",
        "minimum": "最小",
        "maximum": "最大",
    }
    st.html("""
    <style>
      .st-key-analysis_summary [data-testid="stMetricValue"],
      .st-key-analysis_summary [data-testid="stMetricValue"] * {
        white-space: normal !important;
        overflow-wrap: anywhere;
        overflow: visible;
        text-overflow: clip;
      }
    </style>
    """)
    with st.container(key="analysis_summary"):
        metrics = list(labels.items())
        for row in (metrics[:3], metrics[3:]):
            for column, (field, label) in zip(st.columns(len(row)), row, strict=True):
                column.metric(label, str(summary[field]) if summary[field] is not None else "—")
    if not summary["count"]:
        st.info("目前沒有符合條件的已落庫資料。可以調整分類或時間範圍後重新查詢。")
        return
    st.subheader("時間趨勢")
    st.caption("依台灣時間的整點／每日零點分組；無資料的區間保留缺口，不補零。")
    if any(not can_display_taipei(row["timestamp"]) for row in result["trend"]):
        st.warning("部分時間超出台灣日期可顯示範圍，已從趨勢圖略過；統計仍包含這些資料。")
    st.altair_chart(aggregate_trend(result["trend"], result["interval"]), width="stretch")
    system = result["source"] == "realtime"
    st.subheader("設備比較")
    st.altair_chart(category_chart(result["categories"], system=system), width="stretch")
    st.caption("圖表顯示筆數最多的前 20 個設備／舊分類；下表保留全部分類與完整數值。")
    st.dataframe(
        [
            {
                "設備／分類": (
                    "舊資料（未指定設備）"
                    if system and row["category"] == LEGACY
                    else category_label(row["category"])
                ),
                **{label: row[field] for field, label in labels.items()},
            }
            for row in result["categories"]
        ],
        hide_index=True,
        width="stretch",
    )


def render() -> None:
    user = session.get_user()
    if not user:
        st.info("請先登入以查看資料分析。")
        return
    st.title("資料分析")
    st.caption("依來源與設備查看歷史明細、統計與趨勢，並下載 Excel；所有時間使用台灣時間。")
    filters, valid = _filters(user["id"])
    if not valid:
        return
    client = session.get_api_client()
    if "analysis_result" not in st.session_state:
        try:
            st.session_state["analysis_result"] = client.analytics(**filters)
            st.session_state["analysis_loaded_at"] = format_taipei(datetime.now(TAIPEI))
        except APIError as exc:
            clear_results()
            session.handle_api_error(exc)
            if exc.status_code == 422:
                st.info("查詢最多顯示 1,000 個設備／分類與時間區間，請縮小範圍或改用每日分組。")
            return
    system = filters["source"] == "realtime"
    selected = filters.get("device_id" if system else "category", "")
    device_label = (
        _device_label(selected, system)
        if selected in {"", *DEVICE_CATEGORIES, LEGACY}
        else category_label(selected)
    )
    conditions = ""
    if "is_anomaly" in filters:
        conditions += " · 異常：" + ("僅異常" if filters["is_anomaly"] else "正常")
    if filters.get("event_id"):
        conditions += f" · 事件：{filters['event_id']}"
    st.caption(
        f"目前結果：{SYSTEM if system else MINE} · 分類：{device_label} · "
        f"標題：{filters.get('q', '不限')} · "
        f"起：{taipei_iso(filters['start_time']) if filters.get('start_time') else '不限'} · "
        f"迄：{taipei_iso(filters['end_time']) if filters.get('end_time') else '不限'}"
        f"{conditions} · 查詢於 {st.session_state['analysis_loaded_at']}（台灣時間）"
    )
    _display_result(st.session_state["analysis_result"])
    render_history(client, filters)
    st.divider()
    st.subheader("匯出資料")
    st.caption(
        "匯出目前已套用條件下的明細，最多 10,000 筆；匯出時重新查詢資料庫。"
        "Excel 精確數值與台灣時間（+08:00）以文字保存。"
    )
    if st.button("準備 Excel", key="analysis_prepare_export"):
        st.session_state.pop("analysis_export", None)
        try:
            st.session_state["analysis_export"] = client.export_records(**filters)
        except APIError as exc:
            session.handle_api_error(exc)
            if exc.status_code == 422:
                st.info("匯出上限為 10,000 筆，請縮小時間或分類範圍。")
    if data := st.session_state.get("analysis_export"):
        st.download_button(
            "下載 Excel",
            data,
            file_name=f"datapulse-{filters['source']}.xlsx",
            mime=XLSX_MIME,
            key="analysis_download",
            on_click="ignore",
        )
