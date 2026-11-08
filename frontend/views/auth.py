"""Login and registration through FastAPI; tokens stay in the server-side session."""

import streamlit as st

from clients.api import APIClient, APIError
from config import load_config
from state import session


def render() -> None:
    st.title("DataPulse")
    st.write("登入後即可管理資料、查看即時圖表與異常告警。")
    message = st.session_state.pop("auth_notice", None)
    if message:
        st.info(message)
    left, _ = st.columns([1, 1])
    with left:
        login_tab, register_tab = st.tabs(["登入", "註冊"])
        with login_tab, st.form("login_form", clear_on_submit=True):
            email = st.text_input("Email", key="login_email", max_chars=320)
            password = st.text_input("密碼", type="password", key="login_password", max_chars=128)
            submitted = st.form_submit_button("登入", type="primary", width="stretch")
        if submitted:
            if not email.strip() or not password:
                st.error("請輸入 Email 與密碼。")
            else:
                try:
                    client = APIClient(load_config().api_base_url)
                    token = client.login(email.strip(), password)
                    user = APIClient(load_config().api_base_url, token=token["access_token"]).me()
                    session.establish_session(token, user)
                    st.rerun()
                except APIError as exc:
                    st.error(exc.message)
        with register_tab, st.form("register_form", clear_on_submit=True):
            register_email = st.text_input("Email", key="register_email", max_chars=320)
            register_password = st.text_input(
                "密碼（8–128 字元）", type="password", key="register_password", max_chars=128
            )
            confirm = st.text_input("再次輸入密碼", type="password", max_chars=128)
            registered = st.form_submit_button("建立帳號", width="stretch")
        if registered:
            if not register_email.strip() or not 8 <= len(register_password) <= 128:
                st.error("請填寫 Email，並設定 8–128 字元的密碼。")
            elif register_password != confirm:
                st.error("兩次輸入的密碼不一致。")
            else:
                try:
                    APIClient(load_config().api_base_url).register(
                        register_email.strip(), register_password
                    )
                    st.success("帳號已建立，請切換至登入頁籤。")
                except APIError as exc:
                    st.error(exc.message)
        st.caption("新帳號為一般使用者；角色與資料操作權限由後端驗證。")
