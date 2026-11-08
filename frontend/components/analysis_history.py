"""Paginated committed rows using the analysis page's applied source and filters."""

from math import ceil

import streamlit as st

from clients.api import APIError
from state import session
from utils.devices import category_label
from utils.timezone import taipei_iso


def reset_page() -> None:
    st.session_state["analysis_history_page"] = 1


def render(client, filters: dict) -> None:
    st.subheader("歷史明細")
    st.caption(
        "依目前已套用的來源、設備與查詢條件列出已保存資料，時間由新到舊。"
        "統計、分頁與匯出各自查詢；持續新增資料時筆數可能改變，固定結束時間可穩定查詢範圍。"
    )
    size = st.selectbox(
        "每頁筆數", [20, 50, 100], key="analysis_history_size", on_change=reset_page
    )
    page = st.session_state.setdefault("analysis_history_page", 1)
    query = {key: value for key, value in filters.items() if key not in {"source", "interval"}}
    system = filters["source"] == "realtime"
    if system:
        fetch = client.monitoring_samples
    else:
        fetch = client.list_records
        query.update(sort_by="timestamp", sort_order="desc")
    try:
        result = fetch(**query, page=page, page_size=size)
        pages = max(1, ceil(result["total"] / size))
        if page > pages:
            page = pages
            st.session_state["analysis_history_page"] = page
            result = fetch(**query, page=page, page_size=size)
    except APIError as exc:
        session.handle_api_error(exc)
        st.info("歷史明細暫時無法讀取；沒有沿用先前來源的資料。")
        return
    before, count, after = st.columns([1, 3, 1], vertical_alignment="center")
    if before.button("上一頁", key="analysis_history_previous", disabled=page <= 1):
        st.session_state["analysis_history_page"] = page - 1
        st.rerun()
    count.caption(f"共 {result['total']} 筆 · 第 {page} / {pages} 頁")
    if after.button("下一頁", key="analysis_history_next", disabled=page >= pages):
        st.session_state["analysis_history_page"] = page + 1
        st.rerun()
    if not result["items"]:
        st.info("目前條件沒有已保存的歷史資料。")
        return
    rows = []
    for item in result["items"]:
        row = {
            "ID": item["id"],
            "來源": "系統產生" if system else "我的新增／匯入",
            "時間（台灣時間）": taipei_iso(item["timestamp"]),
            "標題": item["title"],
            "設備": (
                "舊資料（未指定設備）"
                if system and item.get("device_id", "legacy") == "legacy"
                else category_label(item["device_id"] if system else item["category"])
            ),
            "數值": item["value"],
        }
        if system:
            row.update(
                {
                    "單位": item.get("unit", ""),
                    "閾值": item["threshold"],
                    "異常": "是" if item["is_anomaly"] else "否",
                    "事件 ID": item["event_id"],
                    "入庫時間（台灣時間）": taipei_iso(item["created_at"]),
                }
            )
        else:
            row["更新時間（台灣時間）"] = taipei_iso(item["updated_at"])
        rows.append(row)
    st.dataframe(rows, hide_index=True, width="stretch")
