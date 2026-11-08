"""Session isolation and monitoring state transitions through real Streamlit reruns."""

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from clients.api import APIError
from clients.realtime import ReceiverSnapshot, SampleEvent, parse_stored_sample
from components.charts import bar_chart, event_frame, trend_chart
from config import FrontendConfig
from state import session
from state.monitor import MonitorBuffer
from views import analysis, auth


def event(value="85", identifier=None, device_id="device-001"):
    return SampleEvent(
        event_id=identifier or str(uuid4()),
        title="Simulation",
        value=Decimal(value),
        category="temperature",
        timestamp=datetime(2026, 11, 8, 4, 0, tzinfo=UTC),
        threshold=Decimal("80"),
        is_anomaly=Decimal(value) > 80,
        device_id=device_id,
        device_name={"device-001": "設備 A", "device-002": "設備 B", "device-003": "設備 C"}.get(
            device_id, "舊版未指定設備"
        ),
        unit="°C" if device_id != "legacy" else "",
    )


class FakeReceiver:
    def __init__(self, *_args, **kwargs):
        self.status = "connected"
        self.device_id = kwargs.get("device_id", "device-001")
        self.events = [
            event("80", device_id=self.device_id),
            replace(
                event("85.123456", device_id=self.device_id),
                timestamp=datetime(2026, 11, 8, 4, 0, 1, tzinfo=UTC),
            ),
        ]
        self.started = 0
        self.stopped = 0
        self.touched = 0
        self.reconnect_count = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1
        self.status = "stopped"

    def touch(self):
        self.touched += 1

    def drain(self):
        events, self.events = self.events, []
        return events

    def snapshot(self):
        return ReceiverSnapshot(self.status, "測試連線狀態", 2, 0, self.reconnect_count, 0, None)


class FakeAPI:
    def __init__(self):
        self.user = {"id": 1, "email": "user@example.com", "role": "user", "is_active": True}
        self.login_calls = []
        self.logout_calls = 0
        self.register_calls = []
        self.login_error = None
        self.me_error = None
        self.logout_error = None
        self.samples = []
        self.sample_calls = []
        self.sample_error = None
        self.device_error = None
        self.on_samples = None

    def monitoring_devices(self):
        if self.device_error:
            raise self.device_error
        return {
            "items": [
                {
                    "id": f"device-00{n}",
                    "name": f"設備 {name}",
                    "category": "temperature",
                    "unit": "°C",
                    "threshold": "80",
                }
                for n, name in enumerate("ABC", 1)
            ]
            + [
                {
                    "id": "legacy",
                    "name": "舊版未指定設備",
                    "category": None,
                    "unit": "",
                    "threshold": None,
                }
            ]
        }

    def monitoring_samples(self, **filters):
        self.sample_calls.append(filters)
        if self.on_samples:
            self.on_samples()
        if self.sample_error:
            raise self.sample_error
        rows = [row for row in self.samples if row["device_id"] == filters["device_id"]]
        if "start_time" in filters:
            rows = [
                row
                for row in rows
                if datetime.fromisoformat(row["timestamp"])
                >= datetime.fromisoformat(filters["start_time"])
            ]
        if "end_time" in filters:
            rows = [
                row
                for row in rows
                if datetime.fromisoformat(row["timestamp"])
                < datetime.fromisoformat(filters["end_time"])
            ]
        if "is_anomaly" in filters:
            rows = [row for row in rows if row["is_anomaly"] == filters["is_anomaly"]]
        rows.sort(key=lambda row: (row["timestamp"], row["id"]), reverse=True)
        page, size = filters["page"], filters["page_size"]
        return {
            "items": rows[(page - 1) * size : page * size],
            "total": len(rows),
            "page": page,
            "page_size": size,
        }

    def login(self, email, password):
        self.login_calls.append((email, password))
        if self.login_error:
            raise self.login_error
        return {"access_token": "test-private-token", "expires_in": 1800}

    def me(self):
        if self.me_error:
            raise self.me_error
        return self.user.copy()

    def logout(self):
        self.logout_calls += 1
        if self.logout_error:
            raise self.logout_error

    def register(self, email, password):
        self.register_calls.append((email, password))
        return self.user.copy()


@pytest.fixture
def setup_app(monkeypatch):
    api = FakeAPI()
    receivers = []

    def make_receiver(*args, **kwargs):
        receiver = FakeReceiver(*args, **kwargs)
        receivers.append(receiver)
        return receiver

    monkeypatch.setenv("API_BASE_URL", "http://api.test")
    monkeypatch.setattr(session, "APIClient", lambda *_a, **_k: api)
    monkeypatch.setattr(auth, "APIClient", lambda *_a, **_k: api)
    monkeypatch.setattr(session, "RealtimeReceiver", make_receiver)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=15)
    return app, api, receivers


def click(app, label):
    return next(button for button in app.button if button.label == label).click().run()


def login(app):
    app.run()
    app.text_input(key="login_email").set_value("user@example.com")
    app.text_input(key="login_password").set_value("secret-password")
    click(app, "登入")
    assert not app.exception


def test_login_reruns_reuse_receiver_and_logout_clears_all_private_state(setup_app):
    app, api, receivers = setup_app
    login(app)
    assert len(receivers) == 1 and receivers[0].started == 1
    assert len(app.get("vega_lite_chart")) == 2
    assert any("1 筆" in error.value for error in app.error)
    assert "login_password" not in app.session_state
    app.session_state["record_edit_99_title"] = "private record"
    app.run()
    assert len(receivers) == 1
    click(app, "登出")
    assert api.logout_calls == 1 and receivers[0].stopped == 1
    state = app.session_state.to_dict()
    assert "auth_token" not in state
    assert "monitor_buffer" not in state and "record_edit_99_title" not in state
    assert any("已登出" in item.value for item in app.info)
    assert not app.exception


def test_wrong_password_never_opens_receiver(setup_app):
    app, api, receivers = setup_app
    api.login_error = APIError(401, "登入資訊不正確。")
    app.run()
    app.text_input(key="login_email").set_value("user@example.com")
    app.text_input(key="login_password").set_value("wrong-password")
    click(app, "登入")
    assert app.error and not receivers
    assert "auth_token" not in app.session_state


def test_logout_network_failure_still_clears_credentials(setup_app):
    app, api, receivers = setup_app
    login(app)
    api.logout_error = APIError(None, "network unavailable")
    click(app, "登出")
    assert "auth_token" not in app.session_state
    assert receivers[0].stopped == 1
    assert any("無法確認伺服器撤銷" in item.value for item in app.info)


@pytest.mark.parametrize("cause", ["expiry", "revocation", "socket"])
def test_auth_expiration_clears_chart_and_stops_receiver(setup_app, cause):
    app, api, receivers = setup_app
    login(app)
    if cause == "expiry":
        app.session_state["auth_expires_at"] = time.time() - 1
    elif cause == "revocation":
        api.me_error = APIError(401, "Expired")
    else:
        receivers[0].status = "auth_failed"
    app.run()
    assert not app.exception
    assert "auth_token" not in app.session_state
    assert "monitor_buffer" not in app.session_state
    assert receivers[0].stopped == 1


def test_pause_resume_resync_and_device_change_stop_old_receivers(setup_app):
    app, api, receivers = setup_app
    login(app)
    click(app, "暫停畫面更新")
    assert receivers[0].stopped == 1
    assert app.session_state["monitor_buffer"].received_count == 2
    assert any("設備仍持續產生與保存資料" in item.value for item in app.info)
    assert next(button for button in app.button if button.label == "重新同步").disabled
    assert len(receivers) == 1
    calls_before = len(api.sample_calls)
    app.run()
    assert len(api.sample_calls) == calls_before
    click(app, "繼續畫面更新")
    assert len(receivers) == 2 and receivers[1].started == 1
    assert len(api.sample_calls) == calls_before + 1
    app.selectbox(key="monitor_device").select("device-002").run()
    assert receivers[1].stopped == 1 and len(receivers) == 3
    assert receivers[2].device_id == "device-002"
    assert {item.device_id for item in app.session_state["monitor_buffer"].recent()} == {
        "device-002"
    }
    click(app, "重新同步")
    assert receivers[2].stopped == 1 and len(receivers) == 4


def test_refresh_identity_uses_latest_role_and_network_failure_hides_data(setup_app):
    app, api, _ = setup_app
    login(app)
    api.user["role"] = "viewer"
    app.run()
    assert any(caption.value == "唯讀使用者" for caption in app.caption)
    api.me_error = APIError(503, "後端暫時不可用")
    app.run()
    assert not app.exception
    assert len(app.metric) == 0
    assert app.error[0].value == "後端暫時不可用"


def test_separate_browser_sessions_do_not_share_credentials_or_buffer(setup_app):
    app, _, _ = setup_app
    login(app)
    other = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=15)
    other.run()
    assert "auth_token" not in other.session_state
    assert "monitor_buffer" not in other.session_state
    assert len(other.metric) == 0
    assert app.session_state["monitor_buffer"].received_count == 2


def test_registration_checks_confirmation_then_calls_api(setup_app):
    app, api, _ = setup_app
    app.run()
    app.text_input(key="register_email").set_value("new@example.com")
    app.text_input(key="register_password").set_value("new-password")
    next(item for item in app.text_input if item.label == "再次輸入密碼").set_value("mismatch")
    click(app, "建立帳號")
    assert not api.register_calls
    app.text_input(key="register_email").set_value("new@example.com")
    app.text_input(key="register_password").set_value("new-password")
    next(item for item in app.text_input if item.label == "再次輸入密碼").set_value("new-password")
    click(app, "建立帳號")
    assert api.register_calls == [("new@example.com", "new-password")]
    assert any("帳號已建立" in item.value for item in app.success)


def test_buffer_deduplicates_and_evicts_both_events_and_ids():
    buffer = MonitorBuffer(capacity=2)
    first = event("80")
    second = replace(event("80.000001"), timestamp=first.timestamp + timedelta(seconds=1))
    third = replace(event("10"), timestamp=first.timestamp + timedelta(seconds=2))
    buffer.append([first, first, second, third])
    assert buffer.received_count == 3
    assert buffer.recent(20) == [second, third]
    assert len(buffer._ids) == 2
    assert [item.is_anomaly for item in buffer.recent()] == [True, False]
    buffer.clear()
    assert buffer.recent() == [] and buffer.received_count == 0


def test_chart_specs_use_fixed_taiwan_time_threshold_and_exact_tooltips():
    sample = event("85.123456")
    line = trend_chart([sample]).to_dict()
    bars = bar_chart([sample]).to_dict()
    assert line["layer"][0]["encoding"]["x"]["scale"]["type"] == "utc"
    assert line["layer"][0]["encoding"]["x"]["title"] == "時間（台灣）"
    values = next(iter(line["datasets"].values()))
    assert values[0]["timestamp"] == "2026-11-08T12:00:00Z"
    assert values[0]["timestamp_utc"] == "2026-11-08T04:00:00+00:00"
    assert values[0]["timestamp_label"] == "2026-11-08 12:00:00 台灣時間"
    assert line["layer"][1]["encoding"]["y"]["field"] == "threshold"
    assert bars["encoding"]["color"]["condition"]["value"] == "#DC3545"
    assert any("value_exact" == tooltip["field"] for tooltip in bars["encoding"]["tooltip"])


def test_chart_date_axis_switches_at_taiwan_midnight_instead_of_utc_midnight():
    before = replace(event(), timestamp=datetime(2026, 11, 8, 15, 59, 59, tzinfo=UTC))
    after = replace(event(), timestamp=datetime(2026, 11, 8, 16, 0, tzinfo=UTC))
    chart = trend_chart([after, before]).to_dict()
    assert chart["layer"][0]["encoding"]["x"]["axis"]["format"] == "%m/%d %H:%M"
    frame = event_frame([after, before])
    assert frame["timestamp"].tolist() == ["2026-11-08T23:59:59Z", "2026-11-09T00:00:00Z"]
    assert frame["segment"].tolist() == [0, 0]

    utc_before = replace(event(), timestamp=datetime(2026, 11, 8, 23, 59, 59, tzinfo=UTC))
    utc_after = replace(event(), timestamp=datetime(2026, 11, 9, 0, 0, tzinfo=UTC))
    same_taiwan_day = trend_chart([utc_before, utc_after]).to_dict()
    assert same_taiwan_day["layer"][0]["encoding"]["x"]["axis"]["format"] == "%H:%M:%S"


def test_charts_skip_out_of_range_taiwan_time_without_losing_valid_events():
    maximum = replace(event(), timestamp=datetime.max.replace(tzinfo=UTC))
    frame = event_frame([event(), maximum])
    assert len(frame) == 1 and frame.iloc[0]["timestamp"] == "2026-11-08T12:00:00Z"
    for chart in (trend_chart([maximum]), bar_chart([maximum])):
        assert next(iter(chart.to_dict()["datasets"].values())) == []


def test_trend_does_not_interpolate_across_a_pause():
    first = event()
    second = replace(event(), timestamp=first.timestamp + timedelta(seconds=1))
    after_pause = replace(event(), timestamp=first.timestamp + timedelta(minutes=2))
    frame = event_frame([first, second, after_pause])
    assert frame["segment"].tolist() == [0, 0, 1]
    assert frame["value_exact"].tolist() == ["85", "85", "85"]


def test_websocket_url_uses_internal_service_and_tls_scheme():
    config = FrontendConfig("https://internal.example/prefix", "https://public.example")
    assert config.websocket_url == "wss://internal.example/prefix/ws/v1/samples"
    assert config.swagger_url == "https://public.example/docs"


def test_paused_fragment_alone_detects_remote_logout(setup_app):
    _, api, _ = setup_app
    app = AppTest.from_string(
        "import streamlit as st\n"
        "from views.monitoring import live_panel\n"
        "if st.session_state.get('auth_token'):\n"
        "    live_panel()\n"
        "else:\n"
        "    st.info('登入已失效')\n",
        default_timeout=15,
    )
    app.session_state["auth_token"] = "test-private-token"
    app.session_state["auth_expires_at"] = time.time() + 1800
    app.session_state["monitor_paused"] = True
    buffer = MonitorBuffer()
    buffer.append([event()])
    app.session_state["monitor_buffer"] = buffer
    app.run()
    assert len(app.metric) == 4
    api.me_error = APIError(401, "Revoked remotely")
    app.session_state["auth_checked_at"] = 0
    app.run()
    assert not app.exception
    assert "auth_token" not in app.session_state
    assert "monitor_buffer" not in app.session_state
    assert len(app.metric) == 0


def stored(sample, identifier=1):
    payload = sample.to_dict()
    payload.pop("persistence")
    return {**payload, "id": identifier, "created_at": sample.timestamp.isoformat()}


def test_initial_history_subscribes_first_and_merges_without_duplicates(setup_app):
    app, api, receivers = setup_app
    historical = replace(event("21.123456"), timestamp=datetime(2026, 11, 8, 3, 59, 50, tzinfo=UTC))

    def before_snapshot():
        assert receivers[0].started == 1
        api.samples = [stored(historical), stored(receivers[0].events[0], 2)]

    api.on_samples = before_snapshot
    login(app)
    assert not app.exception
    events = app.session_state["monitor_buffer"].recent()
    assert [item.value for item in events] == [
        Decimal("21.123456"),
        Decimal("80"),
        Decimal("85.123456"),
    ]
    assert [item.persistence for item in events] == ["committed", "committed", "pending"]
    assert [metric.value for metric in app.metric][2] == "3"
    assert api.sample_calls == [{"device_id": "device-001", "page": 1, "page_size": 300}]


def test_periodic_sync_recovers_delayed_commits_and_reconnection_gap(setup_app):
    app, api, receivers = setup_app
    login(app)
    initial = app.session_state["monitor_buffer"].recent()
    missing = replace(event("25"), timestamp=initial[0].timestamp - timedelta(seconds=1))
    api.samples = [stored(missing), stored(initial[0], 2), stored(initial[1], 3)]
    app.run()
    assert len(api.sample_calls) == 1
    app.session_state["monitor_sync_attempt"] = time.monotonic() - 6
    app.run()
    assert len(api.sample_calls) == 2
    events = app.session_state["monitor_buffer"].recent()
    assert [item.event_id for item in events] == [
        missing.event_id,
        initial[0].event_id,
        initial[1].event_id,
    ]
    assert all(item.persistence == "committed" for item in events)
    after_reconnect = replace(event("28"), timestamp=initial[-1].timestamp + timedelta(seconds=1))
    api.samples.append(stored(after_reconnect, 4))
    receivers[0].reconnect_count = 1
    app.run()
    assert len(api.sample_calls) == 3
    assert app.session_state["monitor_buffer"].recent()[-1].event_id == after_reconnect.event_id


def test_paused_view_catches_up_and_relogin_bootstraps_history(setup_app):
    app, api, receivers = setup_app
    login(app)
    click(app, "暫停畫面更新")
    later = replace(event("42"), timestamp=datetime(2026, 11, 8, 4, 5, tzinfo=UTC))
    api.samples = [stored(later)]
    app.run()
    assert later.event_id not in {
        item.event_id for item in app.session_state["monitor_buffer"].recent()
    }
    assert len(receivers) == 1
    click(app, "繼續畫面更新")
    assert app.session_state["monitor_buffer"].recent()[-1].event_id == later.event_id
    click(app, "登出")
    login(app)
    assert app.session_state["monitor_buffer"].recent()[-1].event_id == later.event_id


@pytest.mark.parametrize("status", [403, 503])
def test_live_sync_failure_is_visible_and_recovers_without_false_healthy_status(setup_app, status):
    app, api, _ = setup_app
    api.sample_error = APIError(status, "歷史暫時無法讀取")
    login(app)
    assert any("畫面可能缺少資料" in item.value for item in app.warning)
    assert not any("🟢" in item.value for item in app.caption)
    assert len(app.metric) == 4
    api.sample_error = None
    app.session_state["monitor_sync_attempt"] = time.monotonic() - 6
    app.run()
    assert "monitor_sync_error" not in app.session_state
    assert any("🟢" in item.value for item in app.caption)


@pytest.mark.parametrize("endpoint", ["samples", "devices"])
def test_initial_history_or_catalog_unauthorized_clears_all_session_state(setup_app, endpoint):
    app, api, receivers = setup_app
    if endpoint == "samples":
        api.sample_error = APIError(401, "Expired")
    else:
        api.device_error = APIError(401, "Expired")
    login(app)
    assert not app.exception
    assert "auth_token" not in app.session_state
    assert "monitor_buffer" not in app.session_state
    assert not app.metric
    assert all(receiver.stopped == 1 for receiver in receivers)


def test_descending_committed_history_charts_in_chronological_order():
    now = datetime(2026, 11, 8, 4, tzinfo=UTC)
    rows = [
        stored(replace(event(str(20 + index)), timestamp=now + timedelta(seconds=index)), index + 1)
        for index in range(50)
    ]
    chart = trend_chart([parse_stored_sample(row) for row in reversed(rows)]).to_dict()
    values = next(iter(chart["datasets"].values()))
    assert {row["segment"] for row in values} == {0}
    assert values[0]["timestamp"] < values[-1]["timestamp"]


@pytest.mark.parametrize("role,device", [("user", "device-002"), ("viewer", "legacy")])
def test_history_entry_navigates_to_analysis_with_device_and_clean_scope(
    setup_app, monkeypatch, role, device
):
    app, api, receivers = setup_app
    api.user["role"] = role

    def analysis_destination():
        st.title("資料分析入口")

    # Keep real Streamlit Page/switch_page routing, replacing only the destination UI.
    monkeypatch.setattr(analysis, "render", analysis_destination)
    login(app)
    app.selectbox(key="monitor_device").select(device).run()
    assert not app.radio and not app.date_input and not app.time_input
    if device == "legacy":
        assert any("不會再產生此類新樣本" in item.value for item in app.info)
    current_receiver = receivers[-1]
    old_keys = {
        "analysis_result": {"source": "records"},
        "analysis_export": b"old workbook",
        "analysis_loaded_at": "old date",
        "analysis_history_page": 5,
        "analysis_history_error": "old error",
        "analysis_filter_start_date": datetime(2025, 1, 1).date(),
        "analysis_filter_start_hour": 9,
        "analysis_filter_q": "old title",
    }
    for key, value in old_keys.items():
        app.session_state[key] = value
    app.session_state["analysis_filters"] = {"source": "records", "created_by": 1}
    app.session_state["analysis_filter_source"] = "我的資料（新增／匯入）"
    receiver_count, request_count = len(receivers), len(api.sample_calls)
    click(app, "查看歷史與分析")
    assert not app.exception and app.title[0].value == "資料分析入口"
    assert app.session_state["analysis_filter_source"] == "系統產生的監控資料"
    assert app.session_state["analysis_filter_category"] == device
    assert app.session_state["analysis_filters"] == {
        "source": "realtime",
        "device_id": device,
        "interval": "day",
    }
    assert app.session_state["analysis_history_page"] == 1
    assert all(key not in app.session_state for key in old_keys if key != "analysis_history_page")
    assert current_receiver.stopped == 1 and len(receivers) == receiver_count
    assert len(api.sample_calls) == request_count
    assert "realtime_receiver" not in app.session_state
    assert app.session_state["monitor_view_open"] is False


def test_monitor_live_labels_use_taiwan_time(setup_app):
    app, _, _ = setup_app
    login(app)
    assert app.metric[3].label == "最近更新（台灣）"
    assert app.metric[3].value == "12:00:01"
    assert app.dataframe[0].value.iloc[0]["時間（台灣）"] == "2026-11-08 12:00:01"
    assert "台灣時間" in app.session_state["monitor_synced_at"]


def test_rest_snapshot_rejects_wrong_device_without_polluting_live_buffer(setup_app):
    app, api, _ = setup_app
    api.monitoring_samples = lambda **_filters: {
        "items": [stored(event(device_id="device-003"))],
        "total": 1,
    }
    login(app)
    assert any("格式不正確" in item.value for item in app.warning)
    assert {item.device_id for item in app.session_state["monitor_buffer"].recent()} == {
        "device-001"
    }


def test_returning_to_monitor_reloads_history_and_navigation_rejects_old_live_tick(setup_app):
    app, api, receivers = setup_app
    login(app)
    prior_calls = len(api.sample_calls)
    # The app marks this false when a different page is selected.
    app.session_state["monitor_view_open"] = False
    app.run()
    assert len(api.sample_calls) == prior_calls + 1
    assert receivers[0].stopped == 1 and len(receivers) == 2
    fragment = AppTest.from_string("from views.monitoring import live_panel\nlive_panel()")
    fragment.session_state["monitor_view_open"] = False
    fragment.run()
    assert not fragment.exception and not fragment.metric
    assert "realtime_receiver" not in fragment.session_state
    assert len(receivers) == 2
