"""Device history and live messages reconcile without losing their persistence state."""

import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import pytest

from clients import realtime
from clients.api import APIClient
from clients.realtime import RealtimeReceiver, parse_sample, parse_stored_sample
from state.monitor import MonitorBuffer


def wire_sample(number=1, **changes):
    return {
        "type": "sample.created",
        "event_id": str(UUID(int=number)),
        "title": "設備 A 溫度",
        "value": "81.123456",
        "category": "temperature",
        "timestamp": "2026-11-08T12:00:00+08:00",
        "threshold": "80",
        "is_anomaly": True,
        "persistence": "pending",
        "device_id": "device-001",
        "device_name": "設備 A",
        "unit": "°C",
        **changes,
    }


def stored_sample(number=1, **changes):
    payload = wire_sample(number)
    payload.pop("type")
    payload.pop("persistence")
    return {**payload, "id": number, "created_at": "2026-11-08T04:00:01Z", **changes}


@pytest.mark.parametrize("marker", [None, "committed"])
def test_stored_row_is_explicitly_committed_without_changing_ws_contract(marker):
    payload = stored_sample(persistence=marker)
    original = payload.copy()
    event = parse_stored_sample(payload)
    assert payload == original
    assert event.persistence == event.to_dict()["persistence"] == "committed"
    assert event.value == Decimal("81.123456")
    assert event.timestamp == datetime(2026, 11, 8, 4, tzinfo=UTC)
    assert (event.device_id, event.device_name, event.unit) == ("device-001", "設備 A", "°C")
    assert event.to_dict()["device_id"] == "device-001"
    assert event.to_dict()["device_name"] == "設備 A"
    assert event.to_dict()["unit"] == "°C"
    with pytest.raises(ValueError, match="persistence"):
        parse_sample({**payload, "type": "sample.created", "persistence": "committed"})
    assert parse_sample(wire_sample()).persistence == "pending"


def test_stored_row_without_marker_and_old_device_fields_has_explicit_legacy_identity():
    payload = stored_sample()
    for field in ("device_id", "device_name", "unit"):
        payload.pop(field)
    historical = parse_stored_sample(payload)
    live = parse_sample({**payload, "type": "sample.created", "persistence": "pending"})
    for event in (historical, live):
        assert (event.device_id, event.device_name, event.unit) == ("legacy", "舊版未指定設備", "")
    assert historical.persistence == "committed" and live.persistence == "pending"


@pytest.mark.parametrize(
    "changes",
    [
        {"id": None},
        {"id": True},
        {"id": "1"},
        {"id": 0},
        {"id": -1},
        {"persistence": "pending"},
        {"persistence": "unknown"},
    ],
)
def test_stored_parser_requires_real_database_id_and_committed_source(changes):
    with pytest.raises(ValueError):
        parse_stored_sample(stored_sample(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"device_id": ""},
        {"device_id": "a" * 65},
        {"device_id": "device 001"},
        {"device_id": "device/001"},
        {"device_id": "設備A"},
        {"device_id": None},
        {"device_id": 1},
        {"device_name": " "},
        {"device_name": "a" * 101},
        {"device_name": None},
        {"unit": "a" * 17},
        {"unit": None},
    ],
)
def test_both_sample_sources_reject_invalid_device_metadata(changes):
    with pytest.raises(ValueError):
        parse_sample(wire_sample(**changes))
    with pytest.raises(ValueError):
        parse_stored_sample(stored_sample(**changes))


def test_monitoring_rest_clients_use_authenticated_routes_and_preserve_all_filters():
    requests = []
    catalog = {"items": [{"id": "device-002", "name": "設備 B", "unit": "°C"}]}
    page = {"items": [stored_sample(device_id="device-002")], "total": 1, "page_size": 300}

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=catalog if request.url.path.endswith("/devices") else page)

    client = APIClient(
        "http://backend:8000", "private-token", transport=httpx.MockTransport(handle)
    )
    assert client.monitoring_devices() == catalog
    assert (
        client.monitoring_samples(
            device_id="device-002",
            category="溫度 A & B",
            page=2,
            page_size=300,
            is_anomaly=False,
            start_time="2026-11-08T00:00:00+08:00",
            end_time=None,
        )
        == page
    )
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/api/v1/monitoring/devices"),
        ("GET", "/api/v1/monitoring/samples"),
    ]
    assert all(request.headers["Authorization"] == "Bearer private-token" for request in requests)
    assert not requests[0].url.query
    assert dict(requests[1].url.params) == {
        "device_id": "device-002",
        "category": "溫度 A & B",
        "page": "2",
        "page_size": "300",
        "is_anomaly": "false",
        "start_time": "2026-11-08T00:00:00+08:00",
    }
    assert all("private-token" not in str(request.url) for request in requests)


@pytest.mark.parametrize(
    "category,device_id,expected",
    [
        (" 溫度 A & B ", "device-002", {"category": ["溫度 A & B"], "device_id": ["device-002"]}),
        (None, "device-003", {"device_id": ["device-003"]}),
        ("temperature", None, {"category": ["temperature"]}),
        (None, None, {}),
    ],
)
def test_receiver_composes_device_and_category_query_without_exposing_token(
    category, device_id, expected
):
    worker = RealtimeReceiver(
        "wss://backend.example/ws/v1/samples",
        "private-token",
        category=category,
        device_id=device_id,
    )
    url = urlsplit(worker.url)
    assert url.scheme == "wss" and url.path == "/ws/v1/samples"
    assert parse_qs(url.query) == expected
    assert worker.device_id == device_id
    assert "private-token" not in worker.url
    assert not worker.is_alive


@pytest.mark.parametrize("device_id", ["", " ", "a" * 65, "device/001", "device 001", "設備A"])
def test_receiver_rejects_invalid_device_filter_before_connecting(device_id):
    with pytest.raises(ValueError, match="device"):
        RealtimeReceiver("ws://backend/ws", "token", device_id=device_id)


@pytest.mark.parametrize(
    "changes",
    [
        {"device_id": "device-003"},
        {"category": "humidity"},
    ],
)
def test_receiver_refuses_events_outside_selected_device_and_category(monkeypatch, changes):
    class Socket:
        def __init__(self):
            self.closed = threading.Event()

        def recv(self, timeout):
            return json.dumps(wire_sample(**changes))

        def close(self):
            self.closed.set()

    socket = Socket()
    calls = []

    def connect(url, **kwargs):
        calls.append((url, kwargs))
        return socket

    monkeypatch.setattr(realtime, "connect", connect)
    worker = RealtimeReceiver(
        "ws://backend/ws/v1/samples",
        "private-token",
        device_id="device-001",
        category="temperature",
        receive_timeout=0.01,
    )
    try:
        worker.start()
        assert socket.closed.wait(2)
        assert worker.stop()
        assert len(calls) == 1
        assert calls[0][1]["additional_headers"] == {"Authorization": "Bearer private-token"}
        assert worker.snapshot().status == "error"
        assert worker.snapshot().invalid_count == 1
        assert worker.drain() == []
    finally:
        worker.stop()


def test_monitor_buffer_sorts_late_history_and_devices_by_time_then_event_id():
    start = datetime(2026, 11, 8, tzinfo=UTC)
    earlier = parse_stored_sample(stored_sample(1, timestamp=start.isoformat()))
    same_time_b = parse_sample(
        wire_sample(3, timestamp=(start + timedelta(seconds=1)).isoformat(), device_id="device-002")
    )
    same_time_a = parse_stored_sample(
        stored_sample(2, timestamp=(start + timedelta(seconds=1)).isoformat())
    )
    latest = parse_sample(wire_sample(4, timestamp=(start + timedelta(seconds=2)).isoformat()))
    buffer = MonitorBuffer(capacity=4)
    buffer.append([latest, same_time_b])
    buffer.append([same_time_a, earlier])
    assert buffer.recent(20) == [earlier, same_time_a, same_time_b, latest]
    assert buffer.received_count == 4


def test_monitor_buffer_promotes_duplicate_to_committed_without_downgrading_or_recounting():
    pending = parse_sample(wire_sample())
    committed = parse_stored_sample(stored_sample())
    buffer = MonitorBuffer()
    buffer.append([pending, pending])
    assert buffer.received_count == 1
    assert buffer.recent() == [pending]
    buffer.append([committed, pending, committed])
    assert buffer.recent() == [committed]
    assert buffer.received_count == 1
    assert buffer.recent()[0].persistence == "committed"


def test_monitor_buffer_keeps_newest_samples_when_old_history_arrives_after_capacity():
    start = datetime(2026, 11, 8, tzinfo=UTC)
    events = [
        parse_stored_sample(
            stored_sample(number, timestamp=(start + timedelta(seconds=number)).isoformat())
        )
        for number in range(1, 6)
    ]
    buffer = MonitorBuffer(capacity=3)
    buffer.append(events[2:])
    buffer.append(reversed(events[:2]))
    assert buffer.recent(100) == events[2:]
    assert buffer.received_count == 5
    assert len(buffer._ids) == 3
    assert buffer.recent(1) == [events[-1]]
    buffer.clear()
    assert buffer.recent() == [] and buffer.received_count == 0
    assert not buffer._ids
