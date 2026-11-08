"""Real reruns keep source, ownership, history, statistics and exports aligned."""

from copy import deepcopy
from datetime import datetime

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from clients.api import APIError
from components.analysis_charts import aggregate_trend, category_chart
from state import session
from state.analysis import MINE, SYSTEM

EVENT = "12345678-1234-4234-8234-123456789012"


def set_time(app, prefix, value):
    instant = datetime.fromisoformat(value)
    app.date_input(key=f"{prefix}_date").set_value(instant.date())
    for suffix in ("hour", "minute", "second"):
        app.selectbox(key=f"{prefix}_{suffix}").select(getattr(instant, suffix))


def submit(app):
    next(button for button in app.button if button.label == "查詢分析").click().run()


def summary(count=2):
    return {
        "count": count,
        "total": "123456789012.123457" if count else "0",
        "average": "61728394506.0617285" if count else None,
        "minimum": "0.000001" if count else None,
        "maximum": "123456789012.123456" if count else None,
    }


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.empty = False
        self.error = None
        self.export_error = None
        self.history_error = None
        self.total = 2

    def last(self, method):
        return next(filters for name, filters in reversed(self.calls) if name == method)

    def analytics(self, **filters):
        self.calls.append(("analytics", deepcopy(filters)))
        if self.error:
            raise self.error
        values = summary(0 if self.empty else 2)
        category = filters.get("device_id", filters.get("category", "device-001"))
        return {
            "source": filters["source"],
            "interval": filters["interval"],
            "summary": values,
            "categories": [] if self.empty else [{"category": category, **values}],
            "trend": [] if self.empty else [{"timestamp": "2026-11-08T00:00:00Z", **values}],
        }

    def history(self, name, filters):
        self.calls.append((name, deepcopy(filters)))
        if self.history_error:
            raise self.history_error
        total = 0 if self.empty else self.total
        offset = (filters["page"] - 1) * filters["page_size"]
        rows = (
            [
                {
                    "id": offset + 1,
                    "title": "sample",
                    "value": "123456789012.123456",
                    "category": filters.get("category", "device-001"),
                    "device_id": filters.get("device_id", "device-001"),
                    "timestamp": "2026-11-07T16:01:02.123456Z",
                    "updated_at": "2026-11-07T16:02:03.123456Z",
                    "created_at": "2026-11-07T16:02:03.123456Z",
                    "threshold": "30.000000",
                    "is_anomaly": filters.get("is_anomaly", True),
                    "unit": "°C",
                    "event_id": EVENT,
                }
            ]
            if total and offset < total
            else []
        )
        return {
            "items": rows,
            "total": total,
            "page": filters["page"],
            "page_size": filters["page_size"],
        }

    def list_records(self, **filters):
        return self.history("records", filters)

    def monitoring_samples(self, **filters):
        return self.history("samples", filters)

    def export_records(self, **filters):
        self.calls.append(("export", deepcopy(filters)))
        if self.export_error:
            raise self.export_error
        return b"PK\x03\x04test"


@pytest.fixture
def harness(monkeypatch):
    client = FakeAPI()
    identity = {"user": {"id": 1, "role": "user"}}
    errors = []

    def handle(exc):
        errors.append(exc)
        if exc.is_unauthorized:
            identity["user"] = None
            st.session_state.clear()
            st.rerun()
        st.error(exc.message)

    monkeypatch.setattr(session, "get_api_client", lambda: client)
    monkeypatch.setattr(session, "get_user", lambda: identity["user"])
    monkeypatch.setattr(session, "handle_api_error", handle)
    app = AppTest.from_string("from views.analysis import render\nrender()", default_timeout=30)
    return app, client, identity, errors


def test_initial_metrics_exact_values_and_empty_results(harness):
    app, client, _, _ = harness
    app.run()
    assert not app.exception
    assert [metric.value for metric in app.metric] == [
        "2",
        "123456789012.123457",
        "61728394506.0617285",
        "0.000001",
        "123456789012.123456",
    ]
    assert app.dataframe[0].value.iloc[0]["總計"] == "123456789012.123457"
    assert app.dataframe[0].value.iloc[0]["設備／分類"] == "設備 A"
    row = app.dataframe[1].value.iloc[0]
    assert row["來源"] == "我的新增／匯入" and row["設備"] == "設備 A"
    assert row["數值"] == "123456789012.123456"
    assert row["時間（台灣時間）"] == "2026-11-08T00:01:02.123456+08:00"
    assert row["更新時間（台灣時間）"] == "2026-11-08T00:02:03.123456+08:00"
    client.empty = True
    submit(app)
    assert not app.exception
    assert [metric.value for metric in app.metric] == ["0", "0", "—", "—", "—"]
    assert not app.dataframe and not app.get("arrow_vega_lite_chart")
    assert any("沒有符合" in message.value for message in app.info)
    assert app.button(key="analysis_history_next").disabled


@pytest.mark.parametrize("role", ["admin", "user", "viewer"])
def test_all_roles_can_select_both_sources_and_personal_data_is_owner_scoped(harness, role):
    app, client, identity, _ = harness
    identity["user"] = {"id": 42, "role": role}
    app.run()
    assert not app.exception
    assert app.selectbox(key="analysis_filter_source").options == [MINE, SYSTEM]
    assert app.selectbox(key="analysis_filter_category").options == [
        "全部設備",
        "設備 A",
        "設備 B",
        "設備 C",
        "指定舊分類",
    ]
    expected = {"source": "records", "created_by": 42, "interval": "day"}
    assert client.last("analytics") == expected
    assert client.last("records") == {
        "created_by": 42,
        "sort_by": "timestamp",
        "sort_order": "desc",
        "page": 1,
        "page_size": 20,
    }
    app.button(key="analysis_prepare_export").click().run()
    assert client.last("export") == expected
    app.selectbox(key="analysis_filter_source").select(SYSTEM).run()
    assert not app.exception
    assert client.last("analytics") == {"source": "realtime", "interval": "day"}
    assert client.last("samples") == {"page": 1, "page_size": 20}
    assert not app.get("download_button")
    assert app.dataframe[1].value.iloc[0]["來源"] == "系統產生"


def test_filters_normalize_time_and_all_personal_views_use_applied_query(harness):
    app, client, _, _ = harness
    app.run()
    app.selectbox(key="analysis_filter_category").select("device-002").run()
    app.text_input(key="analysis_filter_q").set_value(" sample ")
    set_time(app, "analysis_filter_start", "2026-11-08T00:00:00+08:00")
    set_time(app, "analysis_filter_end", "2026-11-09T00:00:00+08:00")
    app.selectbox(key="analysis_filter_interval").select("每小時")
    submit(app)
    expected = {
        "source": "records",
        "interval": "hour",
        "created_by": 1,
        "category": "device-002",
        "q": "sample",
        "start_time": "2026-11-07T16:00:00+00:00",
        "end_time": "2026-11-08T16:00:00+00:00",
    }
    assert not app.exception
    assert client.last("analytics") == expected
    assert client.last("records") == {
        **{key: value for key, value in expected.items() if key not in {"source", "interval"}},
        "sort_by": "timestamp",
        "sort_order": "desc",
        "page": 1,
        "page_size": 20,
    }
    caption = next(item.value for item in app.caption if "目前結果" in item.value)
    assert "起：2026-11-08T00:00:00+08:00" in caption
    assert "迄：2026-11-09T00:00:00+08:00" in caption and "分類：設備 B" in caption
    app.text_input(key="analysis_filter_q").set_value("not applied")
    app.button(key="analysis_prepare_export").click().run()
    assert client.last("export") == expected
    assert len(app.get("download_button")) == 1
    app.run()
    assert len([call for call in client.calls if call[0] == "export"]) == 1


def test_source_and_equipment_switch_immediately_clear_export_and_translate_filter(harness):
    app, client, _, _ = harness
    app.run()
    app.selectbox(key="analysis_filter_category").select("device-003").run()
    app.button(key="analysis_prepare_export").click().run()
    app.selectbox(key="analysis_filter_source").select(SYSTEM).run()
    assert not app.exception
    assert client.last("analytics") == {
        "source": "realtime",
        "interval": "day",
        "device_id": "device-003",
    }
    assert client.last("samples") == {"device_id": "device-003", "page": 1, "page_size": 20}
    assert not app.get("download_button")
    assert app.dataframe[1].value.iloc[0]["設備"] == "設備 C"
    app.button(key="analysis_prepare_export").click().run()
    app.selectbox(key="analysis_filter_category").select("device-001").run()
    assert not app.get("download_button")
    assert client.last("analytics")["device_id"] == "device-001"
    app.selectbox(key="analysis_filter_source").select(MINE).run()
    assert client.last("analytics") == {
        "source": "records",
        "interval": "day",
        "category": "device-001",
        "created_by": 1,
    }


@pytest.mark.parametrize("anomaly,label", [(True, "僅異常"), (False, "正常")])
def test_system_history_filters_match_statistics_export_and_do_not_leak_to_personal(
    harness, anomaly, label
):
    app, client, _, _ = harness
    app.run()
    app.selectbox(key="analysis_filter_source").select(SYSTEM).run()
    app.selectbox(key="analysis_filter_category").select("device-002").run()
    app.text_input(key="analysis_filter_q").set_value(" temperature ")
    set_time(app, "analysis_filter_start", "2026-11-08T00:00:00+08:00")
    set_time(app, "analysis_filter_end", "2026-11-09T00:00:00+08:00")
    app.selectbox(key="analysis_filter_anomaly").select(label)
    app.text_input(key="analysis_filter_event").set_value(EVENT)
    submit(app)
    expected = {
        "source": "realtime",
        "interval": "day",
        "device_id": "device-002",
        "q": "temperature",
        "start_time": "2026-11-07T16:00:00+00:00",
        "end_time": "2026-11-08T16:00:00+00:00",
        "is_anomaly": anomaly,
        "event_id": EVENT,
    }
    assert not app.exception
    assert client.last("analytics") == expected
    assert client.last("samples") == {
        **{key: value for key, value in expected.items() if key not in {"source", "interval"}},
        "page": 1,
        "page_size": 20,
    }
    row = app.dataframe[1].value.iloc[0]
    assert row["異常"] == ("是" if anomaly else "否") and row["事件 ID"] == EVENT
    assert row["入庫時間（台灣時間）"] == "2026-11-08T00:02:03.123456+08:00"
    app.button(key="analysis_prepare_export").click().run()
    assert client.last("export") == expected
    app.selectbox(key="analysis_filter_source").select(MINE).run()
    assert not app.exception
    assert client.last("analytics") == {
        **{
            key: value
            for key, value in expected.items()
            if key not in {"device_id", "is_anomaly", "event_id"}
        },
        "source": "records",
        "created_by": 1,
        "category": "device-002",
    }
    assert not app.get("download_button")
    app.selectbox(key="analysis_filter_source").select(SYSTEM).run()
    assert "event_id" not in client.last("analytics")
    assert "is_anomaly" not in client.last("analytics")
    assert app.selectbox(key="analysis_filter_anomaly").value == "全部"
    assert app.text_input(key="analysis_filter_event").value == ""


@pytest.mark.parametrize("source", [MINE, SYSTEM])
def test_history_pagination_resets_on_equipment_source_size_or_query_change(harness, source):
    app, client, _, _ = harness
    client.total = 101
    app.run()
    app.selectbox(key="analysis_filter_source").select(source).run()
    method = "samples" if source == SYSTEM else "records"
    app.button(key="analysis_history_next").click().run()
    assert not app.exception and client.last(method)["page"] == 2
    assert app.dataframe[1].value.iloc[0]["ID"] == 21
    app.selectbox(key="analysis_filter_category").select("device-003").run()
    assert client.last(method)["page"] == 1
    app.button(key="analysis_history_next").click().run()
    app.selectbox(key="analysis_history_size").select(50).run()
    assert client.last(method)["page"] == 1 and client.last(method)["page_size"] == 50
    app.button(key="analysis_history_next").click().run()
    submit(app)
    assert client.last(method)["page"] == 1
    app.button(key="analysis_history_next").click().run()
    app.selectbox(key="analysis_filter_source").select(MINE if source == SYSTEM else SYSTEM).run()
    assert app.session_state["analysis_history_page"] == 1
    assert not app.exception


def test_history_clamps_page_after_rows_are_deleted(harness):
    app, client, _, _ = harness
    client.total = 45
    app.run()
    app.button(key="analysis_history_next").click().run()
    app.button(key="analysis_history_next").click().run()
    client.total = 2
    app.run()
    assert not app.exception and client.last("records")["page"] == 1
    assert app.session_state["analysis_history_page"] == 1


def test_legacy_categories_remain_explicit_and_never_look_like_equipment(harness):
    app, client, _, _ = harness
    app.run()
    before = len(client.calls)
    app.selectbox(key="analysis_filter_category").select("legacy").run()
    assert not app.exception and not app.metric and len(client.calls) == before
    app.text_input(key="analysis_filter_legacy").set_value(" cpu ")
    submit(app)
    assert client.last("analytics")["category"] == "cpu"
    assert app.dataframe[0].value.iloc[0]["設備／分類"] == "舊分類：cpu"
    app.selectbox(key="analysis_filter_source").select(SYSTEM).run()
    assert not app.exception
    assert client.last("analytics") == {
        "source": "realtime",
        "interval": "day",
        "device_id": "legacy",
    }
    assert app.dataframe[0].value.iloc[0]["設備／分類"] == "舊資料（未指定設備）"
    assert app.dataframe[1].value.iloc[0]["設備"] == "舊資料（未指定設備）"


@pytest.mark.parametrize("source", [MINE, SYSTEM])
def test_invalid_range_hides_previous_metrics_history_and_download(harness, source):
    app, client, _, _ = harness
    app.run()
    app.selectbox(key="analysis_filter_source").select(source).run()
    app.button(key="analysis_prepare_export").click().run()
    before = len(client.calls)
    set_time(app, "analysis_filter_start", "2026-11-09T12:00:00+08:00")
    set_time(app, "analysis_filter_end", "2026-11-08T12:00:00+08:00")
    submit(app)
    assert not app.exception and app.error
    assert not app.metric and not app.dataframe and not app.get("download_button")
    assert len(client.calls) == before
    app.run()
    assert len(client.calls) == before and not app.metric


def test_invalid_event_uuid_never_requests_data_or_leaves_stale_results(harness):
    app, client, _, _ = harness
    app.run()
    app.selectbox(key="analysis_filter_source").select(SYSTEM).run()
    app.button(key="analysis_prepare_export").click().run()
    before = len(client.calls)
    app.text_input(key="analysis_filter_event").set_value("not-a-uuid")
    submit(app)
    assert not app.exception and app.error and len(client.calls) == before
    assert not app.metric and not app.dataframe and not app.get("download_button")


@pytest.mark.parametrize("status", [401, 403, 503])
def test_failed_refresh_clears_previous_results_and_export(harness, status):
    app, client, identity, errors = harness
    app.run()
    app.selectbox(key="analysis_filter_source").select(SYSTEM).run()
    app.button(key="analysis_prepare_export").click().run()
    client.error = APIError(status, "查詢失敗。")
    submit(app)
    assert not app.exception
    assert errors[-1].status_code == status
    assert not app.metric and not app.dataframe and not app.get("download_button")
    if status == 401:
        assert identity["user"] is None


@pytest.mark.parametrize("status", [401, 403, 503])
def test_failed_history_never_shows_previous_source_rows(harness, status):
    app, client, identity, errors = harness
    app.run()
    client.history_error = APIError(status, "歷史查詢失敗。")
    app.selectbox(key="analysis_filter_source").select(SYSTEM).run()
    assert not app.exception and errors[-1].status_code == status
    if status == 401:
        assert identity["user"] is None and not app.dataframe
    else:
        assert len(app.dataframe) == 1  # The fresh system aggregate, no old detail table.
        assert any("歷史明細暫時無法讀取" in message.value for message in app.info)


def test_expired_download_uses_existing_logout_handler(harness):
    app, client, identity, errors = harness
    app.run()
    client.export_error = APIError(401, "登入失效。")
    app.button(key="analysis_prepare_export").click().run()
    assert not app.exception
    assert errors[-1].status_code == 401 and identity["user"] is None
    assert not app.metric and not app.get("download_button")


def test_account_change_clears_owner_filters_results_and_download(harness):
    app, client, identity, _ = harness
    app.run()
    app.selectbox(key="analysis_filter_category").select("device-003").run()
    app.text_input(key="analysis_filter_q").set_value("private title")
    submit(app)
    app.button(key="analysis_prepare_export").click().run()
    identity["user"] = {"id": 2, "role": "user"}
    app.run()
    assert not app.exception
    assert client.last("analytics") == {"source": "records", "interval": "day", "created_by": 2}
    assert client.last("records")["created_by"] == 2
    assert app.text_input(key="analysis_filter_q").value == ""
    assert app.selectbox(key="analysis_filter_category").value == ""
    assert not app.get("download_button")


def test_role_downgrade_keeps_public_monitoring_analysis(harness):
    app, client, identity, _ = harness
    identity["user"]["role"] = "admin"
    app.run()
    app.selectbox(key="analysis_filter_source").select(SYSTEM).run()
    app.selectbox(key="analysis_filter_category").select("device-001").run()
    app.button(key="analysis_prepare_export").click().run()
    identity["user"]["role"] = "viewer"
    app.run()
    assert not app.exception
    assert app.selectbox(key="analysis_filter_source").value == SYSTEM
    assert client.last("samples")["device_id"] == "device-001"
    assert app.get("download_button")


def test_category_chart_displays_device_labels_and_explicit_legacy():
    chart = category_chart(
        [{"category": "device-002", **summary()}, {"category": "legacy", **summary()}], system=True
    ).to_dict()
    points = next(iter(chart["datasets"].values()))
    assert {row["category_label"] for row in points} == {"設備 B", "舊資料（未指定設備）"}
    assert chart["encoding"]["y"]["field"] == "category_label"


def test_trend_uses_taiwan_time_exact_tooltip_and_missing_bucket_gap():
    chart = aggregate_trend(
        [
            {"timestamp": "2026-11-08T01:00:00Z", **summary()},
            {"timestamp": "2026-11-08T03:00:00Z", **summary()},
        ],
        "hour",
    ).to_dict()
    points = next(iter(chart["datasets"].values()))
    assert [point["segment"] for point in points] == [0, 1]
    assert points[0]["average_exact"] == "61728394506.0617285"
    assert points[0]["timestamp_label"].endswith("09:00 台灣時間")
    assert points[0]["timestamp"] == "2026-11-08T09:00:00Z"
    assert points[0]["timestamp_utc"] == "2026-11-08T01:00:00+00:00"
    assert chart["encoding"]["x"]["scale"]["type"] == "utc"


def test_daily_trend_displays_taiwan_midnight_independent_of_browser_timezone():
    chart = aggregate_trend(
        [
            {"timestamp": "2026-11-07T16:00:00Z", **summary()},
            {"timestamp": "2026-11-08T16:00:00Z", **summary()},
        ],
        "day",
    ).to_dict()
    points = next(iter(chart["datasets"].values()))
    assert [point["timestamp"] for point in points] == [
        "2026-11-08T00:00:00Z",
        "2026-11-09T00:00:00Z",
    ]
    assert [point["segment"] for point in points] == [0, 0]
    assert points[0]["timestamp_label"] == "2026-11-08 00:00 台灣時間"
    assert chart["encoding"]["x"]["title"] == "時間（台灣時間）"


def test_trend_skips_unrepresentable_taiwan_timestamp_without_crashing():
    chart = aggregate_trend([{"timestamp": "9999-12-31T23:00:00Z", **summary()}], "hour").to_dict()
    assert not next(iter(chart["datasets"].values()))
