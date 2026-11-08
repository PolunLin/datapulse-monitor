"""Database probes and realtime pipeline counters refreshed on demand."""

import streamlit as st

from clients.api import APIError
from components.admin_shared import handle_error
from utils.timezone import format_taipei


def render(client, _principal: dict) -> None:
    st.subheader("資料庫與即時管線")
    st.button("重新檢查狀態", key="admin_health_refresh")
    try:
        database = client.admin_database()
    except APIError as exc:
        handle_error(exc)
    if database["status"] == "healthy":
        st.success("資料庫連線與查詢正常。")
    else:
        st.error("資料庫檢查失敗，請確認服務狀態後重新檢查。")
    st.caption(
        f"檢查時間（台灣時間）：{format_taipei(database['checked_at'])} · "
        f"MariaDB：{database.get('server_version') or '無法確認'}"
    )
    st.metric("ORM 查詢耗時（毫秒）", f"{database['latency_ms']:.2f}")
    if counts := database.get("counts"):
        labels = {
            "users": "使用者",
            "active_users": "啟用帳號",
            "records": "一般資料",
            "realtime_samples": "即時歷史",
            "system_logs": "系統日誌",
        }
        st.dataframe(
            [{"項目": label, "筆數": counts[name]} for name, label in labels.items()],
            hide_index=True,
            width="stretch",
        )
        latest = database.get("latest_sample_at")
        st.caption(f"最新樣本時間（台灣時間）：{format_taipei(latest) if latest else '尚無資料'}")
    st.subheader("連線池")
    labels = {
        "size": "設定容量",
        "checked_in": "閒置連線",
        "checked_out": "使用中連線",
        "overflow": "目前額外連線",
        "max_overflow": "額外連線上限",
        "timeout_seconds": "等待上限（秒）",
    }
    st.dataframe(
        [{"項目": label, "數值": database["pool"][name]} for name, label in labels.items()],
        hide_index=True,
        width="stretch",
    )
    st.subheader("即時處理狀態")
    st.caption("下列生成、保存與錯誤計數屬於目前程序，服務重啟後歸零。")
    try:
        pipeline = client.monitoring_status()
    except APIError as exc:
        handle_error(exc)
    if not pipeline["writer_running"] or not pipeline["writer_healthy"]:
        st.error("即時資料寫入程序異常，請查看系統日誌。")
    elif pipeline["simulator_enabled"] and not pipeline["generator_running"]:
        st.warning("已啟用模擬資料，但生成器目前未運作。")
    else:
        st.success(
            "即時管線正常。"
            if pipeline["simulator_enabled"]
            else "寫入程序正常，模擬生成器已停用。"
        )
    labels = {
        "generated_count": "已生成",
        "pending_count": "待寫入",
        "persisted_count": "已保存",
        "failure_count": "寫入失敗次數",
        "websocket_connections": "WebSocket 連線",
        "batch_size": "批次筆數",
        "flush_interval_seconds": "寫入間隔（秒）",
    }
    st.dataframe(
        [{"項目": label, "數值": pipeline[name]} for name, label in labels.items()],
        hide_index=True,
        width="stretch",
    )
