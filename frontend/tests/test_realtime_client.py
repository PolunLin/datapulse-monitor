import json
import queue
import threading
import time
from datetime import UTC
from decimal import Decimal
from uuid import uuid4

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from clients import realtime
from clients.realtime import RealtimeReceiver, parse_sample


def sample(**changes):
    return {
        "type": "sample.created",
        "event_id": str(uuid4()),
        "title": "Temperature",
        "value": "81.123456",
        "category": "temperature",
        "timestamp": "2026-11-08T12:00:00+08:00",
        "threshold": "80",
        "is_anomaly": True,
        "persistence": "pending",
        **changes,
    }


class FakeSocket:
    def __init__(self, *messages):
        self.messages = queue.Queue()
        self.closed = threading.Event()
        for message in messages:
            self.messages.put(message)

    def recv(self, timeout):
        try:
            message = self.messages.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        if isinstance(message, Exception):
            raise message
        return json.dumps(message) if isinstance(message, dict) else message

    def close(self):
        self.closed.set()


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


def receiver(**kwargs):
    return RealtimeReceiver(
        "ws://backend:8000/ws/v1/samples",
        "private-token",
        receive_timeout=0.01,
        retry_initial=0.01,
        retry_max=0.04,
        **kwargs,
    )


def test_parse_decimal_utc_and_threshold_boundary():
    event = parse_sample(sample(value="80.000000", is_anomaly=False))
    assert event.value == Decimal("80.000000")
    assert event.timestamp.hour == 4 and event.timestamp.tzinfo == UTC
    assert event.to_dict()["value"] == "80.000000"
    assert event.to_dict()["timestamp"].endswith("+00:00")


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": "not-uuid"},
        {"timestamp": "2026-11-08T12:00:00"},
        {"value": "NaN"},
        {"value": "Infinity"},
        {"value": True},
        {"value": "1000000000000"},
        {"value": "1e999999999"},
        {"value": "1.0000001"},
        {"is_anomaly": "true"},
        {"value": "79", "is_anomaly": True},
        {"category": " "},
        {"persistence": "committed"},
    ],
)
def test_invalid_samples_cannot_reach_charts(changes):
    with pytest.raises(ValueError):
        parse_sample(sample(**changes))


def test_start_is_idempotent_header_auth_and_overflow_are_bounded(monkeypatch):
    events = [sample() for _ in range(4)]
    socket = FakeSocket({"type": "ready"}, *events)
    calls = []

    def connect(url, **kwargs):
        calls.append((url, kwargs))
        return socket

    monkeypatch.setattr(realtime, "connect", connect)
    worker = receiver(queue_capacity=2, category="temperature")
    try:
        worker.start()
        worker.start()
        eventually(lambda: worker.snapshot().received_count == 4)
        assert len(calls) == 1
        assert calls[0][0] == "ws://backend:8000/ws/v1/samples?category=temperature"
        assert calls[0][1]["additional_headers"] == {"Authorization": "Bearer private-token"}
        assert calls[0][1]["proxy"] is None
        assert worker.snapshot().status == "connected"
        assert worker.snapshot().dropped_count == 2
        assert [event.event_id for event in worker.drain()] == [e["event_id"] for e in events[-2:]]
        assert worker.drain() == []
        assert "private-token" not in repr(worker.snapshot())
    finally:
        assert worker.stop()
    assert socket.closed.is_set()
    assert worker.snapshot().status == "stopped"


def test_network_failure_and_server_restart_reconnect(monkeypatch):
    first = FakeSocket(ConnectionClosedError(Close(1012, "restart"), None))
    second = FakeSocket(sample())
    attempts = []

    def connect(*args, **kwargs):
        attempts.append(time.monotonic())
        if len(attempts) == 1:
            raise OSError("private network detail")
        return first if len(attempts) == 2 else second

    monkeypatch.setattr(realtime, "connect", connect)
    worker = receiver()
    try:
        worker.start()
        eventually(lambda: worker.snapshot().received_count == 1)
        assert len(attempts) == 3
        assert worker.snapshot().reconnect_count == 2
        assert attempts[1] - attempts[0] >= 0.009
        assert attempts[2] - attempts[1] >= 0.019
        assert first.closed.is_set()
    finally:
        worker.stop()


def test_websocket_switch_retries_without_losing_authentication_or_queued_events(monkeypatch):
    earlier, later = sample(), sample(value="82")
    first = FakeSocket(
        {"type": "ready"},
        earlier,
        ConnectionClosedOK(Close(1001, "WebSocket service disabled"), None),
    )
    resumed = FakeSocket({"type": "ready"}, later)
    authorizations = []

    def connect(*args, **kwargs):
        authorizations.append(kwargs["additional_headers"])
        if len(authorizations) == 1:
            return first
        if len(authorizations) == 2:
            raise InvalidStatus(Response(503, "Service Unavailable", Headers()))
        return resumed

    monkeypatch.setattr(realtime, "connect", connect)
    worker = receiver()
    try:
        worker.start()
        eventually(lambda: worker.snapshot().received_count == 2)
        assert worker.snapshot().status == "connected"
        assert worker.snapshot().reconnect_count == 2
        assert worker.is_alive
        assert authorizations == [{"Authorization": "Bearer private-token"}] * 3
        assert [event.event_id for event in worker.drain()] == [
            earlier["event_id"],
            later["event_id"],
        ]
        assert first.closed.is_set()
    finally:
        assert worker.stop()
    assert resumed.closed.is_set()


@pytest.mark.parametrize("code", [401, 403])
def test_auth_handshake_rejection_is_terminal(monkeypatch, code):
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise InvalidStatus(Response(code, "denied", Headers()))

    monkeypatch.setattr(realtime, "connect", fail)
    worker = receiver()
    worker.start()
    eventually(lambda: not worker.is_alive)
    assert worker.snapshot().status == "auth_failed"
    assert len(calls) == 1
    assert worker.stop()


def test_revocation_clears_queued_data_and_does_not_retry(monkeypatch):
    socket = FakeSocket(sample(), ConnectionClosedError(Close(1008, "revoked"), None))
    monkeypatch.setattr(realtime, "connect", lambda *a, **kw: socket)
    worker = receiver()
    worker.start()
    eventually(lambda: not worker.is_alive)
    assert worker.snapshot().status == "auth_failed"
    assert worker.snapshot().received_count == 1
    assert worker.snapshot().reconnect_count == 0
    assert worker.drain() == []
    assert socket.closed.is_set()


def test_idle_lease_closes_orphan_and_touch_keeps_active_session(monkeypatch):
    socket = FakeSocket({"type": "ready"})
    monkeypatch.setattr(realtime, "connect", lambda *a, **kw: socket)
    worker = receiver(idle_timeout=0.10)
    worker.start()
    for _ in range(4):
        time.sleep(0.04)
        worker.touch()
        assert worker.is_alive
    eventually(lambda: not worker.is_alive)
    assert socket.closed.is_set()
    assert worker.snapshot().status == "stopped"


def test_malformed_or_wrong_category_event_stops_safely(monkeypatch):
    for message in ["not json", sample(category="humidity")]:
        socket = FakeSocket(message)
        monkeypatch.setattr(realtime, "connect", lambda *a, selected=socket, **kw: selected)
        worker = receiver(category="temperature")
        worker.start()
        eventually(lambda selected=worker: not selected.is_alive)
        assert worker.snapshot().status == "error"
        assert worker.snapshot().invalid_count == 1
        assert worker.drain() == []
        assert socket.closed.is_set()


def test_two_sessions_have_independent_queues_and_credentials(monkeypatch):
    sockets = {"Bearer one": FakeSocket(sample()), "Bearer two": FakeSocket(sample(value="82"))}
    monkeypatch.setattr(
        realtime, "connect", lambda *a, **kw: sockets[kw["additional_headers"]["Authorization"]]
    )
    workers = [
        RealtimeReceiver("ws://backend/ws", token, receive_timeout=0.01) for token in ["one", "two"]
    ]
    try:
        for worker in workers:
            worker.start()
        eventually(lambda: all(w.snapshot().received_count == 1 for w in workers))
        assert workers[0].drain()[0].value == Decimal("81.123456")
        assert workers[1].drain()[0].value == Decimal("82")
    finally:
        for worker in workers:
            worker.stop()


def test_stop_during_connect_remains_bounded_and_eventually_closes(monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    socket = FakeSocket()

    def connect(*args, **kwargs):
        entered.set()
        release.wait(2)
        return socket

    monkeypatch.setattr(realtime, "connect", connect)
    worker = receiver()
    worker.start()
    assert entered.wait(1)
    started = time.monotonic()
    try:
        assert worker.stop(timeout=0.02) is False
        assert time.monotonic() - started < 0.2
    finally:
        release.set()
        eventually(lambda: not worker.is_alive)
    assert socket.closed.is_set()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"queue_capacity": 0},
        {"idle_timeout": float("nan")},
        {"retry_initial": 2, "retry_max": 1},
        {"category": " "},
    ],
)
def test_receiver_rejects_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        RealtimeReceiver("ws://backend/ws", "token", **kwargs)
