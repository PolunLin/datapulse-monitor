"""File selection is separate from mutation; server errors never trigger a retry."""

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from clients.api import APIError, ImportValidationError
from state import session

CONTENT = b"title,value,category\nsample,123456789012.123456,device-001\n"


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.error = None

    def list_records(self, **filters):
        return {"items": [], "total": 0, **filters}

    def import_records(self, filename, content):
        self.calls.append((filename, content))
        if self.error:
            raise self.error
        return {"imported_count": 1}


@pytest.fixture
def harness(monkeypatch):
    client = FakeAPI()
    identity = {"user": {"id": 1, "role": "user"}}

    def handle(exc):
        if exc.is_unauthorized:
            identity["user"] = None
            st.session_state.clear()
            st.rerun()
        st.error(exc.message)

    monkeypatch.setattr(session, "get_api_client", lambda: client)
    monkeypatch.setattr(session, "get_user", lambda: identity["user"])
    monkeypatch.setattr(session, "handle_api_error", handle)
    app = AppTest.from_string("from views.records import render\nrender()", default_timeout=30)
    return app, client, identity


def test_explicit_import_clears_file_and_analysis_and_never_repeats_on_rerun(harness):
    app, client, _ = harness
    app.run()
    assert app.button(key="record_import_submit").disabled
    app.file_uploader[0].set_value(("samples.csv", CONTENT, "text/csv")).run()
    assert not client.calls
    app.session_state["analysis_result"] = {"private": "old"}
    app.session_state["analysis_export"] = b"old"
    app.button(key="record_import_submit").click().run()
    assert not app.exception
    assert client.calls == [("samples.csv", CONTENT)]
    assert app.file_uploader[0].value is None
    assert "analysis_result" not in app.session_state
    assert "analysis_export" not in app.session_state
    assert any("成功匯入 1 筆" in message.value for message in app.success)
    app.run()
    assert len(client.calls) == 1
    app.file_uploader[0].set_value(("renamed.csv", CONTENT, "text/csv")).run()
    assert app.button(key="record_import_submit").disabled
    assert len(client.calls) == 1
    assert any("成功匯入" in message.value for message in app.info)

    second_content = CONTENT.replace(b"sample", b"second")
    app.file_uploader[0].set_value(("second.csv", second_content, "text/csv")).run()
    app.button(key="record_import_submit").click().run()
    assert len(client.calls) == 2
    app.file_uploader[0].set_value(("original.csv", CONTENT, "text/csv")).run()
    assert app.button(key="record_import_submit").disabled
    assert len(client.calls) == 2


def test_structured_row_errors_keep_upload_without_partial_success(harness):
    app, client, _ = harness
    client.error = ImportValidationError(
        [{"列": 2, "欄位": "數值", "原因": "內容不符合欄位格式或範圍。"}], 1, False
    )
    app.run()
    app.file_uploader[0].set_value(("bad.csv", CONTENT, "text/csv")).run()
    app.button(key="record_import_submit").click().run()
    assert not app.exception
    assert app.file_uploader[0].value is not None
    assert app.error and not app.success
    assert app.dataframe[0].value.iloc[0]["列"] == 2
    app.run()
    assert len(client.calls) == 1


def test_viewer_has_no_file_upload_or_import_action(harness):
    app, client, identity = harness
    identity["user"]["role"] = "viewer"
    app.run()
    assert not app.exception
    assert not app.file_uploader
    assert not any(button.label == "確認整批匯入" for button in app.button)
    assert not client.calls


def test_uncertain_import_failure_warns_and_does_not_retry(harness):
    app, client, _ = harness
    client.error = APIError(None, "請求逾時。")
    app.run()
    app.file_uploader[0].set_value(("samples.csv", CONTENT, "text/csv")).run()
    app.button(key="record_import_submit").click().run()
    assert not app.exception
    assert any("無法確認匯入結果" in message.value for message in app.warning)
    app.run()
    assert len(client.calls) == 1


def test_expired_import_clears_upload_and_returns_login_notice(harness):
    app, client, identity = harness
    client.error = APIError(401, "登入失效。")
    app.run()
    app.file_uploader[0].set_value(("samples.csv", CONTENT, "text/csv")).run()
    app.button(key="record_import_submit").click().run()
    assert not app.exception
    assert identity["user"] is None and not app.file_uploader
