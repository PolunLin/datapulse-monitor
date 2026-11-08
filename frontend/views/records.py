"""Authenticated record management. FastAPI remains the authorization boundary."""

from datetime import UTC, datetime
from math import ceil

import streamlit as st

from clients.api import APIError
from components.date_time_fields import time_range_fields
from components.record_form import fields, record_payload
from components.record_import import render as render_import
from state import session
from state.analysis import clear_results
from utils.devices import DEVICE_CATEGORIES, category_label
from utils.timezone import taipei_iso

SORT_OPTIONS = {
    "時間：新到舊": ("timestamp", "desc"),
    "時間：舊到新": ("timestamp", "asc"),
    "數值：高到低": ("value", "desc"),
    "數值：低到高": ("value", "asc"),
    "標題：A–Z": ("title", "asc"),
}


def _reset_selection() -> None:
    st.session_state.pop("records_selected_id", None)
    for key in list(st.session_state):
        if key.startswith("record_selection_"):
            del st.session_state[key]


def _clear_pending_widgets() -> None:
    for kind in ("create", "edit"):
        if st.session_state.pop(f"records_clear_{kind}", False):
            for key in list(st.session_state):
                if key.startswith(f"record_{kind}_"):
                    del st.session_state[key]
    if st.session_state.pop("records_reset_selection", False):
        _reset_selection()


def _filter_time(value: str) -> str | None:
    if not value.strip():
        return None
    if value == "未選擇時間":
        raise ValueError("選擇查詢日期後，請一併選擇時間。")
    try:
        instant = datetime.fromisoformat(value.strip())
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError
        instant = instant.astimezone(UTC)
        if instant.year < 1000:
            raise ValueError
        return instant.isoformat()
    except (ValueError, OverflowError) as exc:
        raise ValueError("查詢時間須包含時區，例如 2026-11-08T12:00:00+08:00。") from exc


def _filters() -> dict:
    st.session_state.setdefault(
        "records_filters",
        {"page_size": 20, "sort_by": "timestamp", "sort_order": "desc"},
    )
    st.session_state.setdefault("records_page", 1)
    with st.form("records_filter_form"):
        keyword, category, size, order = st.columns([2, 2, 1, 2])
        q = keyword.text_input(
            "搜尋標題", max_chars=200, key="record_filter_q", persist_state="session"
        )
        category_choice = category.selectbox(
            "篩選分類",
            ["", *DEVICE_CATEGORIES, "__legacy__"],
            format_func=lambda value: (
                "全部分類"
                if not value
                else "指定舊分類"
                if value == "__legacy__"
                else DEVICE_CATEGORIES[value]
            ),
            key="record_filter_category",
            persist_state="session",
        )
        page_size = size.selectbox(
            "每頁筆數", [20, 50, 100], key="record_filter_page_size", persist_state="session"
        )
        sort_label = order.selectbox(
            "排序", list(SORT_OPTIONS), key="record_filter_sort", persist_state="session"
        )
        with st.expander("時間範圍（台灣時間，選填）"):
            start, end = time_range_fields("record_filter")
        with st.expander("查詢舊分類"):
            legacy_category = st.text_input(
                "舊分類名稱",
                max_chars=100,
                key="record_filter_legacy_category",
                help="篩選分類選擇「指定舊分類」時使用；僅供查詢舊資料。",
                persist_state="session",
            )
        applied = st.form_submit_button("套用篩選", type="primary")
    if applied:
        try:
            category_value = (
                legacy_category.strip() if category_choice == "__legacy__" else category_choice
            )
            if category_choice == "__legacy__" and not category_value:
                raise ValueError("請填寫要查詢的舊分類名稱。")
            start, end = _filter_time(start), _filter_time(end)
            if start and end and datetime.fromisoformat(start) >= datetime.fromisoformat(end):
                raise ValueError("開始時間必須早於結束時間。")
        except ValueError as exc:
            st.error(str(exc))
        else:
            sort_by, sort_order = SORT_OPTIONS[sort_label]
            st.session_state.records_filters = {
                "page_size": page_size,
                "sort_by": sort_by,
                "sort_order": sort_order,
                **({"q": q.strip()} if q.strip() else {}),
                **({"category": category_value.strip()} if category_value.strip() else {}),
                **({"start_time": start} if start else {}),
                **({"end_time": end} if end else {}),
            }
            st.session_state.records_page = 1
            _reset_selection()
    return {**st.session_state.records_filters, "page": st.session_state.records_page}


def _create(client) -> None:
    with st.expander("新增資料"):
        with st.form("record_create_form"):
            values = fields("record_create")
            submitted = st.form_submit_button("建立資料", type="primary")
        if submitted:
            try:
                payload = record_payload(*values)
                client.create_record(payload)
            except ValueError as exc:
                st.error(str(exc))
            except APIError as exc:
                session.handle_api_error(exc)
            else:
                st.session_state.records_clear_create = True
                st.session_state.records_reset_selection = True
                st.session_state.records_page = 1
                clear_results()
                session.flash("資料已建立。")
                st.rerun()


def _detail(client, user: dict, items: list[dict]) -> None:
    names = {record["id"]: f"#{record['id']} · {record['title']}" for record in items}
    options = [None, *names]
    signature = tuple(names.items())
    if st.session_state.get("records_selection_signature") != signature:
        # Native selectboxes may retain the old selected label when only their
        # format_func output changes. Remount when labels change, retaining the ID.
        st.session_state.records_selection_version = (
            st.session_state.get("records_selection_version", 0) + 1
        )
        st.session_state.records_selection_signature = signature
    if st.session_state.get("records_selected_id") not in options:
        _reset_selection()
    widget_key = f"record_selection_{st.session_state.records_selection_version}"
    selected = st.selectbox(
        "選擇資料",
        options,
        index=options.index(st.session_state.get("records_selected_id")),
        key=widget_key,
        format_func=lambda value: "選擇一筆查看或編輯" if value is None else names[value],
    )
    st.session_state.records_selected_id = selected
    if selected is None:
        return
    try:
        record = client.get_record(selected)
    except APIError as exc:
        session.handle_api_error(exc)
        st.info("資料可能已被修改或刪除，請重新整理列表。")
        return
    st.subheader(f"資料 #{record['id']}")
    st.caption(
        f"建立者 ID：{record['created_by']} · "
        f"更新時間（台灣時間）：{taipei_iso(record['updated_at'])}"
    )
    can_edit = user["role"] == "admin" or (
        user["role"] == "user" and user["id"] == record["created_by"]
    )
    if not can_edit:
        st.info("你可以檢視這筆資料；只有建立者或管理員可以修改與刪除。")
        st.json(
            {
                **{key: record[key] for key in ("title", "value", "category")},
                "timestamp": taipei_iso(record["timestamp"]),
            }
        )
        return
    with st.form(f"record_edit_form_{selected}"):
        values = fields(f"record_edit_{selected}", record)
        submitted = st.form_submit_button("儲存變更", type="primary")
    if submitted:
        try:
            if not values[3].strip():
                raise ValueError("修改時時間不可留空。")
            payload = record_payload(*values)
            client.update_record(selected, payload)
        except ValueError as exc:
            st.error(str(exc))
        except APIError as exc:
            session.handle_api_error(exc)
        else:
            st.session_state.records_clear_edit = True
            clear_results()
            session.flash("資料已更新。")
            st.rerun()
    with st.expander("刪除資料"):
        st.warning("刪除後無法復原，請確認這是要移除的資料。")
        confirmed = st.checkbox(
            f"我確認刪除資料 #{selected}", key=f"record_delete_confirm_{selected}"
        )
        if (
            st.button("確認刪除", disabled=not confirmed, key=f"record_delete_{selected}")
            and confirmed
        ):
            try:
                client.delete_record(selected)
            except APIError as exc:
                session.handle_api_error(exc)
            else:
                st.session_state.records_reset_selection = True
                st.session_state.records_clear_edit = True
                if len(items) == 1 and st.session_state.records_page > 1:
                    st.session_state.records_page -= 1
                clear_results()
                session.flash("資料已刪除。")
                st.rerun()


def render() -> None:
    user = session.get_user()
    if not user:
        st.info("請先登入以管理資料。")
        return
    _clear_pending_widgets()
    st.title("資料管理")
    st.caption("查詢與維護工作區資料，數值保留十進位精度，時間使用台灣時間（UTC+8）。")
    session.show_flash()
    client = session.get_api_client()
    if user["role"] in {"user", "admin"}:
        _create(client)
        render_import(client)
    else:
        st.info("目前為檢視者模式，可以查詢資料，無法新增、修改或刪除。")
    filters = _filters()
    st.button("重新整理列表", key="record_refresh")
    try:
        result = client.list_records(**filters)
        page_count = max(1, ceil(result["total"] / filters["page_size"]))
        if filters["page"] > page_count:
            st.session_state.records_page = page_count
            _reset_selection()
            st.rerun()
    except APIError as exc:
        session.handle_api_error(exc)
        return
    items = result["items"]
    previous, summary, following = st.columns([1, 4, 1])
    if previous.button("上一頁", disabled=filters["page"] == 1):
        st.session_state.records_page -= 1
        st.session_state.records_reset_selection = True
        st.rerun()
    summary.caption(f"共 {result['total']} 筆 · 第 {filters['page']} / {page_count} 頁")
    if following.button("下一頁", disabled=filters["page"] >= page_count):
        st.session_state.records_page += 1
        st.session_state.records_reset_selection = True
        st.rerun()
    if not items:
        st.info("目前沒有符合條件的資料。")
        return
    st.dataframe(
        [
            {
                "ID": record["id"],
                "標題": record["title"],
                "數值": record["value"],
                "分類": category_label(record["category"]),
                "時間（台灣時間）": taipei_iso(record["timestamp"]),
                "建立者 ID": record["created_by"],
            }
            for record in items
        ],
        hide_index=True,
        width="stretch",
    )
    _detail(client, user, items)
