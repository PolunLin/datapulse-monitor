"""Exercise native Streamlit forms, roles and reruns without a running browser."""

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.errors import AppTestError

from clients.api import APIError
from components.record_form import record_payload
from state import session


def make_record(record_id: int, *, owner: int = 7) -> dict:
    return {
        "id": record_id,
        "title": f"設備 {record_id}",
        "value": "12.123456",
        "category": "device-001",
        "timestamp": "2026-11-08T04:00:00Z",
        "created_by": owner,
        "created_at": "2026-11-08T04:00:00Z",
        "updated_at": "2026-11-08T04:00:00Z",
    }


class FakeAPI:
    def __init__(self):
        self.records = {1: make_record(1), 2: make_record(2, owner=99)}
        self.calls = []
        self.list_error = None
        self.create_error = None
        self.update_error = None
        self.get_error = None

    def list_records(self, **filters):
        self.calls.append(("list", deepcopy(filters)))
        if self.list_error:
            raise self.list_error
        rows = list(self.records.values())
        if filters.get("q"):
            rows = [row for row in rows if filters["q"] in row["title"]]
        if filters.get("category"):
            rows = [row for row in rows if filters["category"] == row["category"]]
        start = (filters["page"] - 1) * filters["page_size"]
        return {
            "items": deepcopy(rows[start : start + filters["page_size"]]),
            "total": len(rows),
            "page": filters["page"],
            "page_size": filters["page_size"],
        }

    def get_record(self, record_id):
        self.calls.append(("get", record_id))
        if self.get_error:
            raise self.get_error
        return deepcopy(self.records[record_id])

    def create_record(self, payload):
        self.calls.append(("create", deepcopy(payload)))
        if self.create_error:
            raise self.create_error
        record_id = max(self.records, default=0) + 1
        self.records[record_id] = {**make_record(record_id), **payload}
        return deepcopy(self.records[record_id])

    def update_record(self, record_id, payload):
        self.calls.append(("update", record_id, deepcopy(payload)))
        if self.update_error:
            raise self.update_error
        self.records[record_id].update(payload)
        return deepcopy(self.records[record_id])

    def delete_record(self, record_id):
        self.calls.append(("delete", record_id))
        del self.records[record_id]


@pytest.fixture
def harness(monkeypatch):
    client = FakeAPI()
    identity = {"user": {"id": 7, "email": "user@example.com", "role": "user"}}
    errors = []

    def flash(message):
        st.session_state["_test_flash"] = message

    def show_flash():
        if message := st.session_state.pop("_test_flash", None):
            st.success(message)

    def handle_api_error(exc):
        errors.append(exc)
        if exc.is_unauthorized:
            identity["user"] = None
            st.rerun()
        st.error(exc.message)

    monkeypatch.setattr(session, "get_api_client", lambda: client, raising=False)
    monkeypatch.setattr(session, "get_user", lambda: identity["user"], raising=False)
    monkeypatch.setattr(session, "flash", flash, raising=False)
    monkeypatch.setattr(session, "show_flash", show_flash, raising=False)
    monkeypatch.setattr(session, "handle_api_error", handle_api_error, raising=False)
    app = AppTest.from_string(
        "import streamlit as st\nfrom views.records import render\n"
        "if st.session_state.get('test_show_records', True):\n    render()",
        default_timeout=30,
    )
    return app, client, identity, errors


def button(app, label):
    return next(element for element in app.button if element.label == label)


def record_selector(app):
    return next(element for element in app.selectbox if element.label == "選擇資料")


def set_time(app, prefix, value):
    instant = datetime.fromisoformat(value)
    app.date_input(key=f"{prefix}_date").set_value(instant.date())
    for suffix in ("hour", "minute", "second"):
        app.selectbox(key=f"{prefix}_{suffix}").select(getattr(instant, suffix))


def fill_create(app, *, value="123456789012.123456"):
    app.text_input(key="record_create_title").set_value("設備精確測量")
    app.selectbox(key="record_create_category").select("device-001")
    app.text_input(key="record_create_value").set_value(value)
    set_time(app, "record_create_timestamp", "2026-11-08T12:00:00+08:00")


def test_record_edit_and_table_convert_previous_utc_date_without_changing_instant(harness):
    app, client, _, _ = harness
    stamp = "2026-11-07T16:01:02.123456Z"
    client.records[1].update(timestamp=stamp, updated_at=stamp)
    app.run()
    assert not app.exception
    assert app.dataframe[0].value.iloc[0]["時間（台灣時間）"] == (
        "2026-11-08T00:01:02.123456+08:00"
    )
    record_selector(app).select(1).run()
    assert app.date_input(key="record_edit_1_timestamp_date").value == date(2026, 11, 8)
    assert [
        app.selectbox(key=f"record_edit_1_timestamp_{suffix}").value
        for suffix in ("hour", "minute", "second")
    ] == [0, 1, 2]
    assert any(
        "更新時間（台灣時間）：2026-11-08T00:01:02.123456+08:00" in c.value for c in app.caption
    )
    button(app, "儲存變更").click().run()
    assert not app.exception
    assert client.records[1]["timestamp"] == "2026-11-07T16:01:02.123456+00:00"


def test_view_only_record_detail_uses_taiwan_timestamp(harness):
    app, client, identity, _ = harness
    identity["user"]["role"] = "viewer"
    client.records[1]["timestamp"] = "2026-11-07T16:00:00Z"
    app.run()
    record_selector(app).select(1).run()
    assert not app.exception
    assert "2026-11-08T00:00:00+08:00" in app.json[0].value


def test_record_filter_uses_taiwan_midnight_boundary(harness):
    app, client, _, _ = harness
    app.run()
    set_time(app, "record_filter_start", "2026-11-08T00:00:00+08:00")
    set_time(app, "record_filter_end", "2026-11-09T00:00:00+08:00")
    button(app, "套用篩選").click().run()
    assert not app.exception
    filters = next(payload for name, payload in reversed(client.calls) if name == "list")
    assert filters["start_time"] == "2026-11-07T16:00:00+00:00"
    assert filters["end_time"] == "2026-11-08T16:00:00+00:00"


def test_create_read_update_confirm_delete_flow_and_no_rerun_mutations(harness):
    app, client, _, _ = harness
    app.run()
    assert not app.exception
    fill_create(app)
    button(app, "建立資料").click().run()
    assert not app.exception
    created = [call for call in client.calls if call[0] == "create"]
    assert len(created) == 1
    assert created[0][1]["value"] == "123456789012.123456"
    assert created[0][1]["timestamp"] == "2026-11-08T04:00:00+00:00"
    assert app.text_input(key="record_create_title").value == ""
    assert any("已建立" in message.value for message in app.success)

    record_selector(app).select(3).run()
    assert app.text_input(key="record_edit_3_value").value == "123456789012.123456"
    assert button(app, "確認刪除").disabled
    app.text_input(key="record_edit_3_value").set_value("0.100001")
    button(app, "儲存變更").click().run()
    assert not app.exception
    assert client.records[3]["value"] == "0.100001"
    assert any("已更新" in message.value for message in app.success)
    app.checkbox(key="record_delete_confirm_3").check().run()
    assert not button(app, "確認刪除").disabled
    button(app, "確認刪除").click().run()
    assert not app.exception
    assert 3 not in client.records
    assert record_selector(app).value is None
    assert any("已刪除" in message.value for message in app.success)
    app.run()
    assert len([call for call in client.calls if call[0] == "create"]) == 1
    assert len([call for call in client.calls if call[0] == "update"]) == 1
    assert len([call for call in client.calls if call[0] == "delete"]) == 1


def test_viewer_is_read_only_even_for_own_record(harness):
    app, _, identity, _ = harness
    identity["user"]["role"] = "viewer"
    app.run()
    record_selector(app).select(1).run()
    assert not app.exception
    labels = {element.label for element in app.button}
    assert not {"建立資料", "儲存變更", "確認刪除"} & labels
    assert app.json


def test_delete_is_disabled_until_confirmation_is_checked(harness):
    app, client, _, _ = harness
    app.run()
    record_selector(app).select(1).run()
    assert button(app, "確認刪除").disabled
    with pytest.raises(AppTestError, match="disabled"):
        button(app, "確認刪除").click()
    assert not app.exception
    assert 1 in client.records
    assert not any(call[0] == "delete" for call in client.calls)


def test_user_cannot_edit_others_record_but_admin_can(harness):
    app, _, identity, _ = harness
    app.run()
    record_selector(app).select(2).run()
    assert "儲存變更" not in {element.label for element in app.button}
    identity["user"]["role"] = "admin"
    app.run()
    assert "儲存變更" in {element.label for element in app.button}
    assert not app.exception


def test_unauthenticated_page_does_not_call_api(harness):
    app, client, identity, _ = harness
    identity["user"] = None
    app.run()
    assert not app.exception
    assert not client.calls
    assert not app.text_input
    assert "請先登入" in app.info[0].value


def test_filters_reset_pagination_and_send_utc_time_range(harness):
    app, client, _, _ = harness
    client.records = {index: make_record(index) for index in range(1, 24)}
    app.run()
    button(app, "下一頁").click().run()
    assert client.calls[-1][1]["page"] == 2
    assert len(app.dataframe[0].value) == 3
    app.text_input(key="record_filter_q").set_value("23")
    app.selectbox(key="record_filter_category").select("device-001")
    set_time(app, "record_filter_start", "2026-11-08T12:00:00+08:00")
    set_time(app, "record_filter_end", "2026-11-09T12:00:00+08:00")
    app.selectbox(key="record_filter_page_size").select(50)
    app.selectbox(key="record_filter_sort").select("數值：高到低")
    button(app, "套用篩選").click().run()
    assert not app.exception
    assert client.calls[-1][1] == {
        "q": "23",
        "category": "device-001",
        "page": 1,
        "page_size": 50,
        "sort_by": "value",
        "sort_order": "desc",
        "start_time": "2026-11-08T04:00:00+00:00",
        "end_time": "2026-11-09T04:00:00+00:00",
    }
    assert len(app.dataframe[0].value) == 1


def test_deleting_final_item_on_last_page_moves_to_previous_page(harness):
    app, client, _, _ = harness
    client.records = {index: make_record(index) for index in range(1, 22)}
    app.run()
    button(app, "下一頁").click().run()
    record_selector(app).select(21).run()
    app.checkbox(key="record_delete_confirm_21").check().run()
    button(app, "確認刪除").click().run()
    assert not app.exception
    assert app.session_state["records_page"] == 1
    assert len(app.dataframe[0].value) == 20
    assert 21 not in client.records


def test_invalid_input_is_not_submitted_and_failed_request_keeps_input(harness):
    app, client, _, errors = harness
    app.run()
    fill_create(app, value="NaN")
    button(app, "建立資料").click().run()
    assert not any(call[0] == "create" for call in client.calls)
    assert app.error
    client.create_error = APIError(503, "服務暫時無法使用。")
    app.text_input(key="record_create_value").set_value("3.141592")
    button(app, "建立資料").click().run()
    assert not app.exception
    assert errors[-1].status_code == 503
    assert app.text_input(key="record_create_title").value == "設備精確測量"
    assert app.text_input(key="record_create_value").value == "3.141592"


def test_unauthorized_list_routes_to_auth_handler(harness):
    app, client, identity, errors = harness
    client.list_error = APIError(401, "登入已失效。")
    app.run()
    assert not app.exception
    assert errors[-1].status_code == 401
    assert identity["user"] is None
    assert not app.dataframe


def test_forbidden_edit_does_not_show_success_or_clear_unsaved_input(harness):
    app, client, _, errors = harness
    app.run()
    record_selector(app).select(1).run()
    client.update_error = APIError(403, "沒有操作權限。")
    app.text_input(key="record_edit_1_title").set_value("保留未保存變更")
    button(app, "儲存變更").click().run()
    assert not app.exception
    assert errors[-1].status_code == 403
    assert app.text_input(key="record_edit_1_title").value == "保留未保存變更"
    assert client.records[1]["title"] == "設備 1"
    assert not app.success


def test_missing_selected_record_can_refresh_without_crashing(harness):
    app, client, _, errors = harness
    app.run()
    client.get_error = APIError(404, "找不到資料。")
    record_selector(app).select(1).run()
    assert not app.exception
    assert errors[-1].status_code == 404
    del client.records[1]
    client.get_error = None
    button(app, "重新整理列表").click().run()
    assert not app.exception
    assert record_selector(app).value is None


def test_renaming_record_keeps_selection_and_refreshes_dropdown_label(harness):
    app, client, _, _ = harness
    app.run()
    record_selector(app).select(1).run()
    app.text_input(key="record_edit_1_title").set_value("新的設備名稱")
    button(app, "儲存變更").click().run()
    assert not app.exception
    assert client.records[1]["title"] == "新的設備名稱"
    assert record_selector(app).value == 1
    assert "#1 · 新的設備名稱" in record_selector(app).options
    assert "#1 · 設備 1" not in record_selector(app).options
    assert app.text_input(key="record_edit_1_title").value == "新的設備名稱"


def test_filter_widgets_survive_page_cleanup_and_match_applied_query(harness):
    app, client, _, _ = harness
    app.run()
    app.text_input(key="record_filter_q").set_value("2")
    app.selectbox(key="record_filter_category").select("device-001")
    set_time(app, "record_filter_start", "2026-11-08T12:00:00+08:00")
    set_time(app, "record_filter_end", "2026-11-09T12:00:00+08:00")
    app.selectbox(key="record_filter_page_size").select(50)
    app.selectbox(key="record_filter_sort").select("數值：低到高")
    button(app, "套用篩選").click().run()
    query = deepcopy(client.calls[-1][1])
    app.session_state["test_show_records"] = False
    app.run()
    assert not app.text_input
    app.session_state["test_show_records"] = True
    app.run()
    assert not app.exception
    assert app.text_input(key="record_filter_q").value == "2"
    assert app.selectbox(key="record_filter_category").value == "device-001"
    assert app.date_input(key="record_filter_start_date").value == date(2026, 11, 8)
    assert [
        app.selectbox(key=f"record_filter_start_{suffix}").value
        for suffix in ("hour", "minute", "second")
    ] == [12, 0, 0]
    assert app.date_input(key="record_filter_end_date").value == date(2026, 11, 9)
    assert [
        app.selectbox(key=f"record_filter_end_{suffix}").value
        for suffix in ("hour", "minute", "second")
    ] == [12, 0, 0]
    assert app.selectbox(key="record_filter_page_size").value == 50
    assert app.selectbox(key="record_filter_sort").value == "數值：低到高"
    assert client.calls[-1][1] == query


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0.0000001", "1000000000000", "", "1e999"])
def test_record_payload_rejects_values_outside_decimal_storage(value):
    with pytest.raises(ValueError):
        record_payload("t", value, "device-001", "")


def test_record_payload_preserves_decimal_and_requires_timezone():
    payload = record_payload(
        " t ", "999999999999.999999", " device-001 ", "2026-11-08T12:00:00+08:00"
    )
    assert Decimal(payload["value"]) == Decimal("999999999999.999999")
    assert datetime.fromisoformat(payload["timestamp"]).hour == 4
    assert payload["title"] == "t" and payload["category"] == "device-001"
    with pytest.raises(ValueError, match="時間須包含時區"):
        record_payload("t", "1", "device-001", "2026-11-08T12:00:00")


def test_invalid_filter_keeps_applied_query(harness):
    app, client, _, _ = harness
    app.run()
    set_time(app, "record_filter_start", "2026-11-09T12:00:00+08:00")
    set_time(app, "record_filter_end", "2026-11-08T12:00:00+08:00")
    button(app, "套用篩選").click().run()
    assert not app.exception
    assert app.error
    assert "start_time" not in client.calls[-1][1]


def test_legacy_category_is_readable_but_needs_explicit_device_before_edit(harness):
    app, client, _, _ = harness
    client.records[1]["category"] = "temperature"
    app.run()
    assert app.dataframe[0].value.iloc[0]["分類"] == "舊分類：temperature"
    record_selector(app).select(1).run()
    assert app.selectbox(key="record_edit_1_category").value is None
    assert any("原分類「temperature」已保留" in item.value for item in app.info)
    app.text_input(key="record_edit_1_title").set_value("重新分類")
    button(app, "儲存變更").click().run()
    assert not app.exception and app.error
    assert not any(call[0] == "update" for call in client.calls)
    assert client.records[1]["category"] == "temperature"
    app.selectbox(key="record_edit_1_category").select("device-003")
    button(app, "儲存變更").click().run()
    assert not app.exception
    assert client.records[1]["category"] == "device-003"


def test_search_can_select_device_or_explicit_legacy_category(harness):
    app, client, _, _ = harness
    client.records[2]["category"] = "temperature"
    app.run()
    assert app.selectbox(key="record_create_category").options == ["設備 A", "設備 B", "設備 C"]
    app.selectbox(key="record_filter_category").select("device-001")
    button(app, "套用篩選").click().run()
    assert len(app.dataframe[0].value) == 1
    assert app.dataframe[0].value.iloc[0]["分類"] == "設備 A"
    app.selectbox(key="record_filter_category").select("__legacy__")
    app.text_input(key="record_filter_legacy_category").set_value("temperature")
    button(app, "套用篩選").click().run()
    assert not app.exception
    assert client.calls[-1][1]["category"] == "temperature"
    assert app.dataframe[0].value.iloc[0]["ID"] == 2


@pytest.mark.parametrize("invalid", ["abc", "1e3", "NaN", "Infinity", "12.1234567", "1,234"])
def test_numeric_field_rejects_invalid_submissions_without_api_call(harness, invalid):
    app, client, _, _ = harness
    app.run()
    fill_create(app, value=invalid)
    button(app, "建立資料").click().run()
    assert not app.exception and app.error
    assert not any(call[0] == "create" for call in client.calls)


def test_record_date_time_picker_preserves_seconds_and_cross_day_utc(harness):
    app, client, _, _ = harness
    app.run()
    fill_create(app, value="-999999999999.999999")
    set_time(app, "record_create_timestamp", "2026-11-08T00:01:02+08:00")
    button(app, "建立資料").click().run()
    assert not app.exception
    payload = next(call[1] for call in client.calls if call[0] == "create")
    assert payload["timestamp"] == "2026-11-07T16:01:02+00:00"
    assert payload["value"] == "-999999999999.999999"
    assert payload["category"] == "device-001"


def test_time_dropdown_accepts_last_second_of_day(harness):
    app, client, _, _ = harness
    app.run()
    fill_create(app)
    assert not app.time_input
    app.selectbox(key="record_create_timestamp_hour").select(23)
    app.selectbox(key="record_create_timestamp_minute").select(59)
    app.selectbox(key="record_create_timestamp_second").select(59)
    button(app, "建立資料").click().run()
    assert not app.exception
    payload = next(call[1] for call in client.calls if call[0] == "create")
    assert payload["timestamp"] == "2026-11-08T15:59:59+00:00"


def test_query_time_dropdown_rejects_a_missing_minute(harness):
    app, client, _, _ = harness
    app.run()
    app.date_input(key="record_filter_start_date").set_value(date(2026, 11, 8))
    app.selectbox(key="record_filter_start_hour").select(23)
    app.selectbox(key="record_filter_start_second").select(59)
    button(app, "套用篩選").click().run()
    assert not app.exception and app.error
    assert "start_time" not in client.calls[-1][1]


def test_record_date_cannot_be_cleared_to_silently_use_server_time(harness):
    app, client, _, _ = harness
    app.run()
    fill_create(app)
    app.date_input(key="record_create_timestamp_date").set_value(None)
    button(app, "建立資料").click().run()
    assert not app.exception and app.error
    assert not any(call[0] == "create" for call in client.calls)


def test_optional_filter_clears_date_and_time_to_remove_bounds(harness):
    app, client, _, _ = harness
    app.run()
    set_time(app, "record_filter_start", "2026-11-08T00:00:00+08:00")
    button(app, "套用篩選").click().run()
    assert "start_time" in client.calls[-1][1]
    app.date_input(key="record_filter_start_date").set_value(None)
    for suffix in ("hour", "minute", "second"):
        app.selectbox(key=f"record_filter_start_{suffix}").select(None)
    button(app, "套用篩選").click().run()
    assert not app.exception and not app.error
    assert "start_time" not in client.calls[-1][1]


def test_date_without_time_rejects_query_without_replacing_applied_filter(harness):
    app, client, _, _ = harness
    app.run()
    app.date_input(key="record_filter_start_date").set_value(date(2026, 11, 8))
    for suffix in ("hour", "minute", "second"):
        app.selectbox(key=f"record_filter_start_{suffix}").select(None)
    button(app, "套用篩選").click().run()
    assert not app.exception and app.error
    assert "start_time" not in client.calls[-1][1]


def test_upper_bound_record_remains_readable_and_preserves_original_utc_on_edit(harness):
    app, client, _, _ = harness
    stamp = "9999-12-31T23:59:59.999999Z"
    client.records[1]["timestamp"] = stamp
    app.run()
    assert not app.exception
    assert stamp in app.dataframe[0].value.iloc[0]["時間（台灣時間）"]
    assert "UTC；超出台灣時間可顯示範圍" in app.dataframe[0].value.iloc[0]["時間（台灣時間）"]
    record_selector(app).select(1).run()
    assert any("原時間超出台灣日期可顯示範圍" in item.value for item in app.warning)
    app.text_input(key="record_edit_1_title").set_value("保留原始時間")
    button(app, "儲存變更").click().run()
    assert not app.exception
    assert client.records[1]["timestamp"] == "9999-12-31T23:59:59.999999+00:00"
