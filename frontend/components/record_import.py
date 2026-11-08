"""Explicit, all-or-nothing file imports; a rerun never repeats a submitted file."""

from hashlib import sha256

import streamlit as st

from clients.api import APIError, ImportValidationError
from state import session
from state.analysis import clear_results


def render(client) -> None:
    with st.expander("批次匯入 CSV／JSON"):
        st.caption(
            "UTF-8 CSV（支援 BOM）或 JSON 陣列，最多 2 MiB／5,000 筆。"
            "必填：title、value、category；category 僅接受 device-001（設備 A）、"
            "device-002（設備 B）、device-003（設備 C）。"
            "timestamp 選填，提供時須包含時區，例如 2026-11-08T12:00:00+08:00。"
            "數值最多 12 位整數、6 位小數；JSON 建議以字串保存數值。"
        )
        st.caption(
            "所有列通過驗證後才會整批寫入，建立者為目前登入帳號。"
            "CSV 錯誤列號含標題列；JSON 從第 1 筆起算。"
        )
        version = st.session_state.get("record_import_version", 0)
        uploaded = st.file_uploader(
            "選擇匯入檔案",
            type=["csv", "json"],
            max_upload_size=2,
            key=f"record_import_file_{version}",
        )
        content = uploaded.getvalue() if uploaded is not None else None
        digest = sha256(content).hexdigest() if content is not None else None
        completed = st.session_state.get("record_import_completed", set())
        duplicate = digest is not None and digest in completed
        too_large = content is not None and len(content) > 2 * 1024 * 1024
        if duplicate:
            st.info("這份內容已在本次登入中成功匯入。請選擇新的檔案，避免重複建立資料。")
        if too_large:
            st.error("檔案過大，請將檔案縮小至 2 MiB 以內。")
        submitted = st.button(
            "確認整批匯入",
            key="record_import_submit",
            disabled=uploaded is None or duplicate or too_large,
        )
        if not submitted or uploaded is None or duplicate or too_large:
            return
        try:
            result = client.import_records(uploaded.name, content)
        except ImportValidationError as exc:
            st.error(exc.message)
            st.dataframe(exc.errors, hide_index=True, width="stretch")
            st.caption(
                f"共 {exc.total_errors} 個錯誤；"
                + (f"顯示 {len(exc.errors)} 個。" if exc.truncated else "已顯示全部。")
            )
        except APIError as exc:
            session.handle_api_error(exc)
            if exc.status_code is None or exc.status_code >= 500:
                st.warning("無法確認匯入結果；請先重新查詢資料，確認未寫入後再重試，避免重複資料。")
        else:
            st.session_state["record_import_completed"] = completed | {digest}
            st.session_state["record_import_version"] = version + 1
            st.session_state["records_reset_selection"] = True
            st.session_state["records_page"] = 1
            clear_results()
            session.flash(f"已成功匯入 {result['imported_count']} 筆資料。")
            st.rerun()
