"""Role and activation changes require a fresh detail read and explicit confirmation."""

import streamlit as st

from clients.api import APIError
from components.admin_shared import handle_error, list_page, set_filters
from utils.timezone import taipei_iso

ROLES = {"管理員": "admin", "一般使用者": "user", "唯讀使用者": "viewer"}
STATUSES = {"全部": None, "啟用": True, "停用": False}


def render(client, principal: dict) -> None:
    st.subheader("使用者與角色")
    st.caption("角色或啟用狀態變更後，該帳號既有登入憑證會被撤銷。")
    with st.form("admin_users_filters_form"):
        keyword, role_column, active_column, size_column = st.columns([3, 2, 2, 1])
        q = keyword.text_input(
            "搜尋 Email", max_chars=320, key="admin_users_q", persist_state="session"
        )
        role = role_column.selectbox(
            "角色", ["全部", *ROLES], key="admin_users_role_filter", persist_state="session"
        )
        active = active_column.selectbox(
            "帳號狀態", list(STATUSES), key="admin_users_active_filter", persist_state="session"
        )
        size = size_column.selectbox(
            "每頁筆數", [20, 50, 100], key="admin_users_size", persist_state="session"
        )
        if st.form_submit_button("查詢使用者", type="primary"):
            set_filters(
                "admin_users",
                {
                    "page_size": size,
                    **({"q": q.strip()} if q.strip() else {}),
                    **({"role": ROLES[role]} if role != "全部" else {}),
                    **({"is_active": STATUSES[active]} if STATUSES[active] is not None else {}),
                },
            )
    st.button("重新整理使用者", key="admin_users_refresh")
    items = list_page(client.admin_users, "admin_users")
    if not items:
        return
    labels = {value: label for label, value in ROLES.items()}
    st.dataframe(
        [
            {
                "ID": item["id"],
                "Email": item["email"],
                "角色": labels[item["role"]],
                "狀態": "啟用" if item["is_active"] else "停用",
                "建立時間（台灣時間）": taipei_iso(item["created_at"]),
            }
            for item in items
        ],
        hide_index=True,
        width="stretch",
    )
    names = {item["id"]: f"#{item['id']} · {item['email']}" for item in items}
    if st.session_state.get("admin_users_selected") not in names:
        st.session_state.pop("admin_users_selected", None)
    selected = st.selectbox(
        "選擇使用者",
        [None, *names],
        key="admin_users_selected",
        format_func=lambda value: "選擇帳號以查看權限" if value is None else names[value],
    )
    if st.session_state.get("admin_users_editor_target") != selected:
        st.session_state["admin_users_editor_target"] = selected
        st.session_state.pop("admin_users_editor_signature", None)
    if selected is None:
        return
    try:
        item = client.admin_user(selected)
    except APIError as exc:
        handle_error(exc)
    st.caption(f"帳號 #{item['id']} · 更新時間（台灣時間）：{taipei_iso(item['updated_at'])}")
    if selected == principal["id"]:
        st.info("這是目前登入的管理員帳號；自己的角色與啟用狀態須由另一位管理員調整。")
        return
    signature = (selected, item["role"], item["is_active"], item["updated_at"])
    if st.session_state.get("admin_users_editor_signature") != signature:
        for key in list(st.session_state):
            if key.startswith("admin_users_edit_"):
                st.session_state.pop(key, None)
        # Deleting Session State alone does not reset browser-held form inputs.
        # Remount the complete editor so confirmation belongs to this exact version.
        st.session_state["admin_users_editor_version"] = (
            st.session_state.get("admin_users_editor_version", 0) + 1
        )
        st.session_state["admin_users_editor_signature"] = signature
    editor = f"admin_users_edit_{selected}_{st.session_state['admin_users_editor_version']}"
    with st.form(f"{editor}_form"):
        role = st.selectbox(
            "設定角色",
            list(ROLES),
            index=list(ROLES.values()).index(item["role"]),
            key=f"{editor}_role",
        )
        active = st.checkbox("允許帳號登入", value=item["is_active"], key=f"{editor}_active")
        confirmed = st.checkbox("我確認變更此帳號，並撤銷其既有登入。", key=f"{editor}_confirm")
        submitted = st.form_submit_button("儲存使用者變更", type="primary")
    if not submitted:
        return
    if not confirmed:
        st.warning("請先勾選確認，再儲存使用者變更。")
        return
    payload = {"role": ROLES[role], "is_active": active}
    if all(payload[field] == item[field] for field in payload):
        st.info("角色與啟用狀態沒有變更。")
        return
    try:
        client.update_admin_user(selected, payload)
    except APIError as exc:
        handle_error(exc)
    else:
        st.session_state.pop("admin_users_editor_signature", None)
        st.session_state["admin_notice"] = "使用者設定已更新，該帳號的既有登入憑證已撤銷。"
        st.rerun()
