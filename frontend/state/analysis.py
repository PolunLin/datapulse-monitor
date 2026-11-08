"""Invalidate session-owned queries and provide one history-navigation entry point."""

import streamlit as st

MINE = "我的資料（新增／匯入）"
SYSTEM = "系統產生的監控資料"
SOURCES = {MINE: "records", SYSTEM: "realtime"}


def clear_results() -> None:
    for key in ("analysis_result", "analysis_export", "analysis_loaded_at"):
        st.session_state.pop(key, None)


def open_system_history(device_id: str | None = None) -> None:
    for key in list(st.session_state):
        if key.startswith("analysis_"):
            del st.session_state[key]
    st.session_state["analysis_filter_source"] = SYSTEM
    st.session_state["analysis_filter_category"] = device_id or ""
    st.session_state["analysis_filters"] = {
        "source": "realtime",
        "interval": "day",
        **({"device_id": device_id} if device_id else {}),
    }
    st.session_state["analysis_history_page"] = 1
