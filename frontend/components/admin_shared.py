"""Shared pagination and validation for the Admin pages; no data response cache."""

from datetime import datetime
from math import ceil

import streamlit as st

from clients.api import APIError
from components.date_time_fields import time_range_fields
from state import session
from state.admin import clear_admin_state
from views.records import _filter_time


def handle_error(exc: APIError) -> None:
    if exc.status_code == 403:
        clear_admin_state()
    session.handle_api_error(exc)
    st.stop()


def time_range(start: str, end: str) -> dict:
    start, end = _filter_time(start), _filter_time(end)
    if start and end and datetime.fromisoformat(start) >= datetime.fromisoformat(end):
        raise ValueError("開始時間必須早於結束時間。")
    return {
        **({"start_time": start} if start else {}),
        **({"end_time": end} if end else {}),
    }


def time_fields(prefix: str) -> tuple[str, str]:
    return time_range_fields(prefix)


def set_filters(prefix: str, filters: dict) -> None:
    st.session_state[f"{prefix}_filters"] = filters
    st.session_state[f"{prefix}_page"] = 1
    st.session_state.pop(f"{prefix}_invalid", None)
    st.session_state.pop(f"{prefix}_selected", None)


def invalid_filters(prefix: str, message: str | None = None) -> bool:
    if message:
        st.session_state[f"{prefix}_invalid"] = message
    if message := st.session_state.get(f"{prefix}_invalid"):
        st.error(message)
        return True
    return False


def list_page(fetch, prefix: str) -> list[dict]:
    filters = st.session_state.get(f"{prefix}_filters", {"page_size": 20})
    current = st.session_state.get(f"{prefix}_page", 1)
    try:
        result = fetch(**filters, page=current)
        pages = max(1, ceil(result["total"] / filters["page_size"]))
        if current > pages:
            current = pages
            st.session_state[f"{prefix}_page"] = current
            st.session_state.pop(f"{prefix}_selected", None)
            result = fetch(**filters, page=current)
    except APIError as exc:
        handle_error(exc)
    left, center, right = st.columns([1, 4, 1])
    if left.button("上一頁", key=f"{prefix}_previous", disabled=current <= 1):
        st.session_state[f"{prefix}_page"] = current - 1
        st.session_state.pop(f"{prefix}_selected", None)
        st.rerun()
    center.caption(f"共 {result['total']} 筆 · 第 {current} / {pages} 頁")
    if right.button("下一頁", key=f"{prefix}_next", disabled=current >= pages):
        st.session_state[f"{prefix}_page"] = current + 1
        st.session_state.pop(f"{prefix}_selected", None)
        st.rerun()
    if not result["items"]:
        st.info("目前沒有符合條件的資料。")
    return result["items"]
