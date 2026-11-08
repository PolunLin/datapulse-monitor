"""Streamlit controls exercise authorization, confirmations and fresh Admin queries."""

import time
from copy import deepcopy
from datetime import datetime

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from clients.api import APIError
from state import session

STAMP = "2026-11-08T01:00:00Z"
EVENT = "95ed8f1a-df1e-43d0-afdc-2a7101e662b5"


def user(identifier, role="user"):
    return {
        "id": identifier,
        "email": f"user-{identifier}@example.com",
        "role": role,
        "is_active": True,
        "created_at": STAMP,
        "updated_at": STAMP,
    }


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.users = [user(1, "admin"), user(2)]
        self.errors = {}
        self.history = [
            {
                "id": 7,
                "event_id": EVENT,
                "title": "CPU",
                "value": "123456789012.123456",
                "threshold": "80.000000",
                "category": "cpu",
                "is_anomaly": True,
                "timestamp": STAMP,
                "created_at": STAMP,
                "device_id": "device-001",
                "device_name": "設備 A",
                "unit": "°C",
            }
        ]
        self.logs = [
            {
                "id": 9,
                "timestamp": STAMP,
                "level": "INFO",
                "kind": "audit",
                "logger": "app.admin",
                "message": "User permissions changed",
                "request_id": "request-123",
                "method": None,
                "path": None,
                "status_code": None,
                "duration_ms": None,
                "error_type": None,
                "actor_id": 1,
                "target_user_id": 2,
                "action": "user.updated",
                "changes": {"role": {"before": "user", "after": "viewer"}},
            }
        ]
        self.db = {
            "status": "healthy",
            "checked_at": STAMP,
            "latency_ms": 1.234,
            "server_version": "11.7.2",
            "counts": {
                "users": 2,
                "active_users": 2,
                "records": 10,
                "realtime_samples": 20,
                "system_logs": 3,
            },
            "latest_sample_at": STAMP,
            "pool": {
                "size": 5,
                "checked_in": 2,
                "checked_out": 0,
                "overflow": 0,
                "max_overflow": 5,
                "timeout_seconds": 30,
            },
            "error": None,
        }

    def call(self, name, payload=None):
        self.calls.append((name, deepcopy(payload)))
        if name in self.errors:
            raise self.errors[name]

    def page(self, items, filters):
        page, size = filters["page"], filters["page_size"]
        return {
            "items": deepcopy(items[(page - 1) * size : page * size]),
            "total": len(items),
            "page": page,
            "page_size": size,
        }

    def admin_users(self, **filters):
        self.call("users", filters)
        items = [
            row
            for row in self.users
            if (not filters.get("q") or filters["q"] in row["email"])
            and (not filters.get("role") or filters["role"] == row["role"])
            and ("is_active" not in filters or filters["is_active"] == row["is_active"])
        ]
        return self.page(items, filters)

    def admin_user(self, identifier):
        self.call("user", identifier)
        return deepcopy(next(row for row in self.users if row["id"] == identifier))

    def update_admin_user(self, identifier, payload):
        self.call("update", {"id": identifier, **payload})
        row = next(row for row in self.users if row["id"] == identifier)
        row.update(payload)
        row["updated_at"] = "2026-11-08T02:00:00Z"
        return deepcopy(row)

    def admin_logs(self, **filters):
        self.call("logs", filters)
        return self.page(self.logs, filters)

    def admin_logging_status(self):
        self.call("logging_status")
        return {
            "queue_capacity": 1000,
            "pending_count": 2,
            "dropped_count": 0,
            "persisted_count": 10,
            "failure_count": 0,
            "last_error": None,
            "retained_limit": 10000,
            "writer_running": True,
        }

    def admin_database(self):
        self.call("database")
        return deepcopy(self.db)

    def monitoring_status(self):
        self.call("pipeline")
        return {
            "simulator_enabled": True,
            "generator_running": True,
            "writer_running": True,
            "writer_healthy": True,
            "generated_count": 21,
            "pending_count": 1,
            "persisted_count": 20,
            "failure_count": 0,
            "last_error": None,
            "websocket_connections": 2,
            "batch_size": 100,
            "flush_interval_seconds": 5,
        }

    def monitoring_history(self, **filters):
        self.call("history", filters)
        return self.page(self.history, filters)


@pytest.fixture
def harness(monkeypatch):
    api = FakeAPI()
    identity = {"user": user(1, "admin")}
    errors = []

    def error(exc):
        errors.append(exc)
        if exc.is_unauthorized:
            identity["user"] = None
            st.session_state.clear()
            st.rerun()
        st.error(exc.message)

    monkeypatch.setattr(session, "get_api_client", lambda: api)
    monkeypatch.setattr(session, "get_user", lambda: identity["user"])
    monkeypatch.setattr(session, "handle_api_error", error)
    app = AppTest.from_string("from views.admin import render\nrender()", default_timeout=15)
    return app, api, identity, errors


def click(app, label):
    next(item for item in app.button if item.label == label).click().run()


def section(app, name):
    app.selectbox(key="admin_section").select(name).run()
    assert not app.exception


def role_editor(app):
    return next(item for item in app.selectbox if item.label == "設定角色")


def confirmation(app):
    return next(
        item for item in app.checkbox if item.label == "我確認變更此帳號，並撤銷其既有登入。"
    )


def active_editor(app):
    return next(item for item in app.checkbox if item.label == "允許帳號登入")


@pytest.mark.parametrize("role", ["user", "viewer", None])
def test_non_admin_never_requests_management_data_and_clears_state(harness, role):
    app, api, identity, _ = harness
    identity["user"] = user(1, role) if role else None
    app.session_state["admin_private"] = "private"
    app.run()
    assert not app.exception and not api.calls and app.warning
    assert "admin_private" not in app.session_state


def test_only_selected_section_fetches_data_and_filters_are_applied(harness):
    app, api, _, _ = harness
    app.run()
    assert [name for name, _ in api.calls] == ["users"]
    app.text_input(key="admin_users_q").set_value("user-2")
    app.selectbox(key="admin_users_role_filter").select("一般使用者")
    click(app, "查詢使用者")
    assert api.calls[-1] == ("users", {"page_size": 20, "page": 1, "q": "user-2", "role": "user"})
    assert len(app.dataframe[0].value) == 1
    section(app, "資料庫狀態")
    assert [name for name, _ in api.calls[-2:]] == ["database", "pipeline"]
    assert app.metric[0].value == "1.23"


def test_user_change_requires_confirmation_and_refreshes_detail_after_success(harness):
    app, api, _, _ = harness
    app.run()
    app.selectbox(key="admin_users_selected").select(2).run()
    role_editor(app).select("唯讀使用者")
    active_editor(app).uncheck()
    click(app, "儲存使用者變更")
    assert app.warning and not any(name == "update" for name, _ in api.calls)
    old_confirmation_key = confirmation(app).key
    confirmation(app).check()
    click(app, "儲存使用者變更")
    assert not app.exception
    assert ("update", {"id": 2, "role": "viewer", "is_active": False}) in api.calls
    assert api.users[1]["role"] == "viewer" and not api.users[1]["is_active"]
    assert app.success and not confirmation(app).value
    assert confirmation(app).key != old_confirmation_key
    role_editor(app).select("一般使用者")
    click(app, "儲存使用者變更")
    assert app.warning
    assert len([call for call in api.calls if call[0] == "update"]) == 1


def test_editor_confirmation_is_new_for_target_switch_and_external_update(harness):
    app, api, _, _ = harness
    api.users.append(user(3))
    app.run()
    app.selectbox(key="admin_users_selected").select(2).run()
    confirmation(app).check().run()
    original_key = confirmation(app).key
    app.selectbox(key="admin_users_selected").select(3).run()
    assert not confirmation(app).value and confirmation(app).key != original_key
    app.selectbox(key="admin_users_selected").select(2).run()
    assert not confirmation(app).value and confirmation(app).key != original_key
    confirmation(app).check().run()
    previous_key = confirmation(app).key
    api.users[1].update(role="viewer", updated_at="2026-11-08T03:00:00Z")
    click(app, "重新整理使用者")
    assert not app.exception
    assert role_editor(app).value == "唯讀使用者"
    assert not confirmation(app).value and confirmation(app).key != previous_key


def test_self_account_has_no_mutation_form(harness):
    app, api, _, _ = harness
    app.run()
    app.selectbox(key="admin_users_selected").select(1).run()
    assert not app.exception
    assert not any(button.label == "儲存使用者變更" for button in app.button)
    assert any("目前登入的管理員" in item.value for item in app.info)
    assert not any(name == "update" for name, _ in api.calls)


def test_user_pages_and_empty_results_clear_previous_selection(harness):
    app, api, _, _ = harness
    api.users = [user(n, "admin" if n == 1 else "user") for n in range(1, 23)]
    app.run()
    app.selectbox(key="admin_users_selected").select(2).run()
    click(app, "下一頁")
    assert api.calls[-1] == ("users", {"page_size": 20, "page": 2})
    assert app.selectbox(key="admin_users_selected").value is None
    assert len(app.dataframe[0].value) == 2
    app.text_input(key="admin_users_q").set_value("missing")
    click(app, "查詢使用者")
    assert not app.dataframe and app.info
    assert api.calls[-1][1]["page"] == 1


def test_logs_search_normalizes_time_and_shows_audit_detail(harness):
    app, api, _, _ = harness
    app.run()
    section(app, "系統日誌")
    app.selectbox(key="admin_logs_kind").select("管理操作")
    app.selectbox(key="admin_logs_level").select("INFO")
    app.text_input(key="admin_logs_actor").set_value("1")
    app.text_input(key="admin_logs_request").set_value("request-123")
    app.date_input(key="admin_logs_start_date").set_value(datetime(2026, 11, 8).date())
    for suffix, value in zip(("hour", "minute", "second"), (9, 0, 0), strict=True):
        app.selectbox(key=f"admin_logs_start_{suffix}").select(value)
    click(app, "查詢日誌")
    filters = api.calls[-1][1]
    assert filters["kind"] == "audit" and filters["level"] == "INFO"
    assert filters["actor_id"] == 1 and filters["request_id"] == "request-123"
    assert filters["start_time"] == "2026-11-08T01:00:00+00:00"
    app.selectbox(key="admin_logs_selected").select(9).run()
    assert "user.updated" in app.json[0].value


@pytest.mark.parametrize("field,value", [("admin_logs_actor", "0"), ("range", "reversed")])
def test_invalid_log_filters_hide_old_results_without_request(harness, field, value):
    app, api, _, _ = harness
    app.run()
    section(app, "系統日誌")
    count = len(api.calls)
    if field == "range":
        app.date_input(key="admin_logs_start_date").set_value(datetime(2026, 11, 9).date())
        app.date_input(key="admin_logs_end_date").set_value(datetime(2026, 11, 8).date())
    else:
        app.text_input(key=field).set_value(value)
    click(app, "查詢日誌")
    assert not app.exception and app.error and not app.dataframe
    assert len(api.calls) == count
    app.run()
    assert len(api.calls) == count


def test_history_entry_opens_analysis_for_all_devices_without_duplicate_query(harness, monkeypatch):
    app, api, _, _ = harness
    destinations = []
    monkeypatch.setattr(st, "switch_page", destinations.append)
    app.run()
    section(app, "即時歷史")
    assert any("歷史資料查詢已整合至" in item.value for item in app.info)
    assert not app.dataframe and not app.text_input and not app.date_input
    assert not any(name == "history" for name, _ in api.calls)
    for key, value in {
        "analysis_result": {"source": "records"},
        "analysis_export": b"old workbook",
        "analysis_filter_category": "device-002",
        "analysis_history_page": 3,
        "analysis_validation_error": "old range",
    }.items():
        app.session_state[key] = value
    click(app, "前往資料分析")
    assert not app.exception and len(destinations) == 1
    assert destinations[0].url_path == "analysis"
    assert app.session_state["analysis_filters"] == {"source": "realtime", "interval": "day"}
    assert app.session_state["analysis_filter_source"] == "系統產生的監控資料"
    assert app.session_state["analysis_filter_category"] == ""
    assert app.session_state["analysis_history_page"] == 1
    assert all(
        key not in app.session_state
        for key in ("analysis_result", "analysis_export", "analysis_validation_error")
    )
    assert not any(name == "history" for name, _ in api.calls)


def test_database_unavailable_does_not_show_stale_counts(harness):
    app, api, _, _ = harness
    app.run()
    section(app, "資料庫狀態")
    assert len(app.dataframe) == 3
    api.db.update(status="unavailable", counts=None, latest_sample_at=None, server_version=None)
    click(app, "重新檢查狀態")
    assert not app.exception and app.error
    assert len(app.dataframe) == 2
    assert not any("資料庫連線與查詢正常" in item.value for item in app.success)


def test_admin_timestamps_show_taiwan_date_in_tables_details_and_health(harness):
    app, api, _, _ = harness
    stamp = "2026-11-07T16:01:02.123456Z"
    local = "2026-11-08T00:01:02.123456+08:00"
    for item in api.users:
        item.update(created_at=stamp, updated_at=stamp)
    api.logs[0]["timestamp"] = stamp
    api.db.update(checked_at=stamp, latest_sample_at=stamp)
    app.run()
    assert not app.exception
    assert app.dataframe[0].value.iloc[0]["建立時間（台灣時間）"] == local
    app.selectbox(key="admin_users_selected").select(2).run()
    assert any(f"更新時間（台灣時間）：{local}" in c.value for c in app.caption)
    section(app, "系統日誌")
    assert app.dataframe[0].value.iloc[0]["時間（台灣時間）"] == local
    app.selectbox(key="admin_logs_selected").select(9).run()
    assert local in app.json[0].value
    section(app, "資料庫狀態")
    assert not app.exception
    assert any("檢查時間（台灣時間）：2026-11-08 00:01:02" in c.value for c in app.caption)
    assert any("最新樣本時間（台灣時間）：2026-11-08 00:01:02" in c.value for c in app.caption)


@pytest.mark.parametrize("status", [401, 403, 503])
def test_auth_and_network_errors_hide_previously_rendered_logs(harness, status):
    app, api, identity, errors = harness
    app.run()
    section(app, "系統日誌")
    app.session_state["admin_private"] = "private"
    api.errors["logs"] = APIError(status, "查詢失敗")
    click(app, "重新整理日誌")
    assert not app.exception and not app.dataframe
    assert errors[-1].status_code == status
    if status in {401, 403}:
        assert "admin_private" not in app.session_state
    if status == 401:
        assert identity["user"] is None


def test_full_session_validation_purges_admin_but_keeps_monitoring_analysis_on_downgrade(
    monkeypatch,
):
    class Client:
        def me(self):
            return user(1, "viewer")

    monkeypatch.setattr(session, "get_api_client", lambda: Client())
    app = AppTest.from_string("from state.session import validate_session\nvalidate_session()")
    app.session_state["auth_token"] = "private"
    app.session_state["auth_expires_at"] = time.time() + 100
    app.session_state["auth_user"] = user(1, "admin")
    app.session_state["admin_users_selected"] = 2
    app.session_state["admin_section"] = "系統日誌"
    app.session_state["analysis_filters"] = {
        "source": "realtime",
        "category": "cpu",
        "interval": "day",
        "device_id": "device-001",
    }
    app.session_state["analysis_filter_device"] = "device-001"
    app.session_state["analysis_result"] = {"secret": "history"}
    app.session_state["analysis_export"] = b"private"
    app.run()
    assert not app.exception
    state = app.session_state.to_dict()
    assert not any(key.startswith("admin_") for key in state)
    assert state["analysis_result"] == {"secret": "history"}
    assert state["analysis_export"] == b"private"
    assert state["analysis_filter_device"] == "device-001"
    assert state["analysis_filters"] == {
        "source": "realtime",
        "category": "cpu",
        "interval": "day",
        "device_id": "device-001",
    }
