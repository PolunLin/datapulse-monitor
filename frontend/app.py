"""Compose Streamlit pages; FastAPI remains authoritative for permissions."""

import streamlit as st

from config import load_config
from state import session
from views import admin, analysis, auth, monitoring, records

st.set_page_config(page_title="DataPulse", page_icon=":material/monitoring:", layout="wide")

try:
    config = load_config()
except ValueError as exc:
    st.error(str(exc))
    st.stop()

st.html("""
<style>
  .stMainBlockContainer { padding: 1.5rem 2rem 2rem; }
  h1 { font-size: 2rem !important; }
  h3 { font-size: 1.25rem !important; }
  [data-testid="stMetricValue"] { font-size: 1.8rem; }
  [data-testid="stSidebar"] { border-right: 1px solid #E1E6ED; }
  @media (max-width: 640px) {
    .stMainBlockContainer { padding-top: 4rem; }
  }
</style>
""")

authenticated = session.validate_session()
if authenticated:
    monitor_page = st.Page(
        monitoring.render,
        title="即時監控",
        icon=":material/monitoring:",
        url_path="home",
        default=True,
    )
    monitor_alias_page = st.Page(monitoring.render, title="即時監控", url_path="monitoring")
    record_page = st.Page(
        records.render, title="資料管理", icon=":material/table_view:", url_path="records"
    )
    analysis_page = st.Page(
        analysis.render, title="資料分析", icon=":material/analytics:", url_path="analysis"
    )
    user = session.get_user()

    def restricted_admin() -> None:
        st.warning("目前帳號沒有系統管理權限，請從側邊欄選擇可使用的頁面。")

    admin_page = st.Page(
        admin.render if user["role"] == "admin" else restricted_admin,
        title="系統管理",
        icon=":material/admin_panel_settings:",
        url_path="admin",
    )
    # Keep known URLs registered across login/logout and role changes, while
    # selecting a safe renderer before Streamlit resolves the current route.
    pages = [monitor_page, monitor_alias_page, record_page, analysis_page, admin_page]
    page = st.navigation(pages, position="hidden")
    with st.sidebar:
        st.title("DataPulse")
        st.page_link(monitor_page, label="即時監控", icon=":material/monitoring:")
        st.page_link(record_page, label="資料管理", icon=":material/table_view:")
        st.page_link(analysis_page, label="資料分析", icon=":material/analytics:")
        if user["role"] == "admin":
            st.page_link(admin_page, label="系統管理", icon=":material/admin_panel_settings:")
        st.divider()
        user = session.get_user()
        st.text(user["email"])
        st.caption({"admin": "管理員", "user": "一般使用者", "viewer": "唯讀使用者"}[user["role"]])
        if st.button("登出", width="stretch"):
            session.logout()
    if page not in (monitor_page, monitor_alias_page):
        session.stop_receiver()
        st.session_state["monitor_view_open"] = False
else:
    session.stop_receiver()
    page = st.navigation(
        [
            st.Page(auth.render, title="登入", url_path="home", default=True),
            *[
                st.Page(auth.render, title="登入", url_path=route)
                for route in ("monitoring", "records", "analysis", "admin")
            ],
        ],
        position="hidden",
    )

page.run()
