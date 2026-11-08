"""A single history entry point shared with the analysis page."""

import streamlit as st

from state import session
from state.analysis import open_system_history


def render(_client, _principal: dict) -> None:
    st.subheader("即時歷史資料")
    st.info(
        "歷史資料查詢已整合至「資料分析」，可依設備、時間與異常狀態查詢，並查看統計與下載 Excel。"
    )
    st.caption("選擇「系統產生的監控資料」查看已落庫的設備歷史；也可切換查看自己新增或匯入的資料。")
    if st.button("前往資料分析", key="admin_history_open_analysis", type="primary"):
        from views import analysis

        session.stop_receiver()
        st.session_state["monitor_view_open"] = False
        open_system_history()
        st.switch_page(st.Page(analysis.render, title="資料分析", url_path="analysis"))
