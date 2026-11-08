"""Only the selected Admin section is queried on each Streamlit rerun."""

import streamlit as st

from components import admin_health, admin_history, admin_logs, admin_users
from state import session
from state.admin import clear_admin_state

SECTIONS = {
    "使用者管理": admin_users.render,
    "系統日誌": admin_logs.render,
    "資料庫狀態": admin_health.render,
    "即時歷史": admin_history.render,
}


def render() -> None:
    user = session.get_user()
    if not user or user.get("role") != "admin":
        clear_admin_state()
        st.warning("系統管理僅供管理員使用。")
        return
    st.title("系統管理")
    st.caption("管理帳號權限、查詢系統運作；歷史資料由資料分析統一查詢。")
    section = st.selectbox("管理功能", list(SECTIONS), key="admin_section", persist_state="session")
    if notice := st.session_state.pop("admin_notice", None):
        st.success(notice)
    SECTIONS[section](session.get_api_client(), user)
