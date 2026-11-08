"""Persisted application and audit log search, with ingestion status made visible."""

import streamlit as st

from clients.api import APIError
from components.admin_shared import (
    handle_error,
    invalid_filters,
    list_page,
    set_filters,
    time_fields,
    time_range,
)
from utils.timezone import taipei_iso

KINDS = {"全部": None, "HTTP 請求": "request", "系統運作": "runtime", "管理操作": "audit"}


def render(client, _principal: dict) -> None:
    st.subheader("系統日誌查詢")
    st.caption("查詢已保存的應用程式與管理操作紀錄；時間使用台灣時間（UTC+8）。")
    with st.form("admin_logs_filters_form"):
        a, b, c = st.columns([2, 2, 3])
        level = a.selectbox(
            "等級",
            ["全部", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            key="admin_logs_level",
            persist_state="session",
        )
        kind = b.selectbox("日誌類型", list(KINDS), key="admin_logs_kind", persist_state="session")
        q = c.text_input("訊息關鍵字", max_chars=200, key="admin_logs_q", persist_state="session")
        a, b, c = st.columns([3, 2, 1])
        request_id = a.text_input(
            "Request ID", max_chars=64, key="admin_logs_request", persist_state="session"
        )
        actor_id = b.text_input(
            "操作人 ID", max_chars=20, key="admin_logs_actor", persist_state="session"
        )
        size = c.selectbox(
            "每頁筆數", [20, 50, 100], key="admin_logs_size", persist_state="session"
        )
        start, end = time_fields("admin_logs")
        submitted = st.form_submit_button("查詢日誌", type="primary")
    if submitted:
        try:
            if actor_id.strip() and (not actor_id.strip().isdecimal() or int(actor_id) < 1):
                raise ValueError("操作人 ID 須為正整數。")
            filters = {
                "page_size": size,
                **time_range(start, end),
                **({"level": level} if level != "全部" else {}),
                **({"kind": KINDS[kind]} if KINDS[kind] else {}),
                **({"q": q.strip()} if q.strip() else {}),
                **({"request_id": request_id.strip()} if request_id.strip() else {}),
                **({"actor_id": int(actor_id)} if actor_id.strip() else {}),
            }
        except ValueError as exc:
            invalid_filters("admin_logs", str(exc))
            return
        set_filters("admin_logs", filters)
    if invalid_filters("admin_logs"):
        return
    st.button("重新整理日誌", key="admin_logs_refresh")
    try:
        status = client.admin_logging_status()
    except APIError as exc:
        handle_error(exc)
    st.caption(
        f"定期保留最近 {status['retained_limit']:,} 筆 · 待寫入 {status['pending_count']} / "
        f"{status['queue_capacity']} · 本程序已保存 {status['persisted_count']} 筆 · "
        f"省略 {status['dropped_count']} 筆 · 寫入失敗 {status['failure_count']} 次"
    )
    if not status["writer_running"] or status["failure_count"] or status["dropped_count"]:
        st.warning("日誌收集曾發生省略或寫入異常，查詢結果可能不完整；請確認資料庫與服務狀態。")
    items = list_page(client.admin_logs, "admin_logs")
    if not items:
        return
    st.dataframe(
        [
            {
                "ID": row["id"],
                "時間（台灣時間）": taipei_iso(row["timestamp"]),
                "等級": row["level"],
                "類型": row["kind"],
                "訊息": row["message"],
                "HTTP": row.get("status_code"),
                "操作人 ID": row.get("actor_id"),
                "Request ID": row.get("request_id"),
            }
            for row in items
        ],
        hide_index=True,
        width="stretch",
    )
    indexed = {row["id"]: row for row in items}
    if st.session_state.get("admin_logs_selected") not in indexed:
        st.session_state.pop("admin_logs_selected", None)
    selected = st.selectbox(
        "查看日誌明細",
        [None, *indexed],
        key="admin_logs_selected",
        format_func=lambda value: "選擇一筆日誌" if value is None else f"#{value}",
    )
    if selected is not None:
        st.json(
            {
                **indexed[selected],
                "timestamp": taipei_iso(indexed[selected]["timestamp"]),
            },
            expanded=True,
        )
