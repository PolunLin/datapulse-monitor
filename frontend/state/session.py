"""Session-owned credentials and workers. Authenticated objects are never cached globally."""

import time

import streamlit as st

from clients.api import APIClient, APIError
from clients.realtime import RealtimeReceiver
from config import load_config
from state.admin import retain_authorized_state


def get_api_client() -> APIClient:
    return APIClient(load_config().api_base_url, token=st.session_state.get("auth_token"))


def get_user() -> dict:
    return st.session_state.get("auth_user", {})


def stop_receiver() -> None:
    receiver = st.session_state.pop("realtime_receiver", None)
    st.session_state.pop("realtime_receiver_scope", None)
    if receiver is not None:
        receiver.stop()
        st.session_state["monitor_sync_due"] = True


def clear_session(message: str | None = None) -> None:
    try:
        stop_receiver()
    finally:
        st.session_state.clear()
        if message:
            st.session_state["auth_notice"] = message


def establish_session(token: dict, user: dict) -> None:
    clear_session()
    st.session_state["auth_token"] = token["access_token"]
    st.session_state["auth_expires_at"] = time.time() + token["expires_in"]
    st.session_state["auth_user"] = user


def session_expired() -> bool:
    return time.time() >= st.session_state.get("auth_expires_at", 0)


def expire_session() -> None:
    clear_session("登入已過期或失效，請重新登入。")
    st.rerun()


def handle_api_error(exc: APIError) -> None:
    if exc.is_unauthorized:
        expire_session()
    else:
        st.error(exc.message)


def validate_session() -> bool:
    if not st.session_state.get("auth_token"):
        return False
    if session_expired():
        expire_session()
    try:
        st.session_state["auth_user"] = get_api_client().me()
        retain_authorized_state(st.session_state["auth_user"])
        st.session_state["auth_checked_at"] = time.monotonic()
        st.session_state["auth_verification_pending"] = False
    except APIError as exc:
        handle_api_error(exc)
        st.info("暫時無法確認登入狀態，恢復連線後請重新整理。")
        if st.button("重新連線"):
            st.rerun()
        if st.button("清除本機登入"):
            clear_session("已清除本機登入，伺服器上的憑證仍依原到期時間失效。")
            st.rerun()
        st.stop()
    return True


def verify_paused_session() -> bool:
    """Keep revocation checks alive while the WebSocket is intentionally closed."""
    if time.monotonic() - st.session_state.get("auth_checked_at", 0) >= 5:
        st.session_state["auth_checked_at"] = time.monotonic()
        try:
            st.session_state["auth_user"] = get_api_client().me()
            retain_authorized_state(st.session_state["auth_user"])
            st.session_state["auth_verification_pending"] = False
        except APIError as exc:
            st.session_state["auth_verification_pending"] = True
            handle_api_error(exc)
    if st.session_state.get("auth_verification_pending", False):
        st.warning("暫時無法確認登入狀態，已隱藏監控資料；連線恢復後會自動重試。")
        return False
    return True


def logout() -> None:
    message = "已登出，這個帳號的既有登入憑證已撤銷。"
    try:
        get_api_client().logout()
    except APIError as exc:
        if exc.is_unauthorized:
            message = "登入憑證已失效，已清除本機登入。"
        else:
            message = "已清除本機登入；目前無法確認伺服器撤銷，既有憑證仍可能有效至到期。"
    finally:
        clear_session(message)
    st.rerun()


def flash(message: str) -> None:
    st.session_state["flash_message"] = message


def show_flash() -> None:
    message = st.session_state.pop("flash_message", None)
    if message:
        st.success(message)


def get_receiver(category: str | None = None, device_id: str | None = None) -> RealtimeReceiver:
    receiver = st.session_state.get("realtime_receiver")
    scope = (category, device_id)
    if receiver is not None and st.session_state.get("realtime_receiver_scope") != scope:
        stop_receiver()
        receiver = None
    if receiver is not None and receiver.snapshot().status == "stopped":
        stop_receiver()
        receiver = None
    if receiver is None:
        receiver = RealtimeReceiver(
            load_config().websocket_url,
            st.session_state["auth_token"],
            category=category,
            device_id=device_id,
        )
        st.session_state["realtime_receiver"] = receiver
        st.session_state["realtime_receiver_scope"] = scope
        st.session_state["monitor_sync_due"] = True
        receiver.start()
    receiver.touch()
    return receiver
