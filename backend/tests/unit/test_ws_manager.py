import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import WebSocket
from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import UnauthorizedError
from app.realtime.events import SampleEvent
from app.services.auth import Principal
from app.ws.manager import ConnectionManager


def principal() -> Principal:
    now = datetime.now(UTC)
    return Principal(1, "viewer@example.com", "viewer", True, 0, now, now)


def event(category: str = "temperature", *, device_id: str = "legacy") -> SampleEvent:
    return SampleEvent(
        title="temperature sample",
        value=Decimal("90.000000"),
        category=category,
        device_id=device_id,
        threshold=Decimal("80.000000"),
        is_anomaly=True,
    )


class FakeAuth:
    def __init__(self) -> None:
        self.revoked = False
        self.database_down = False

    async def authenticate(self, token: str) -> Principal:
        if self.database_down:
            raise SQLAlchemyError("database unavailable")
        if self.revoked:
            raise UnauthorizedError("revoked")
        return principal()


class FakeSocket:
    def __init__(self, *, block_samples=False, block_close=False, block_accept=False) -> None:
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.sample_started = asyncio.Event()
        self.closed = asyncio.Event()
        self.accept_started = asyncio.Event()
        self.accept_release = asyncio.Event()
        self.close_release = asyncio.Event()
        self.block_samples = block_samples
        self.block_close = block_close
        self.block_accept = block_accept
        self.close_code = None
        self.close_reason = None
        self.accepted = False

    async def accept(self) -> None:
        self.accept_started.set()
        if self.block_accept:
            await self.accept_release.wait()
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        if payload["type"] == "sample.created":
            self.sample_started.set()
            if self.block_samples:
                await asyncio.Event().wait()
        self.outgoing.put_nowait(payload)

    async def receive(self) -> dict:
        return await self.incoming.get()

    async def close(self, code: int, reason: str) -> None:
        self.close_code = code
        self.close_reason = reason
        self.closed.set()
        if self.block_close:
            await self.close_release.wait()


async def connect(manager, websocket, *, category=None, device_id=None):
    task = asyncio.create_task(manager.serve(websocket, "token", principal(), category, device_id))
    ready = await asyncio.wait_for(websocket.outgoing.get(), timeout=1)
    assert ready == {
        "type": "ready",
        "category": category,
        "device_id": device_id,
        "persistence": "pending",
    }
    return task


async def test_slow_client_overflow_does_not_block_other_clients():
    manager = ConnectionManager(FakeAuth(), queue_capacity=1, send_timeout=1)
    slow, fast = FakeSocket(block_samples=True), FakeSocket()
    tasks = [await connect(manager, slow), await connect(manager, fast)]
    try:
        first, second, third = event(), event(), event()
        await manager.publish(first)
        await asyncio.wait_for(slow.sample_started.wait(), timeout=1)
        assert (await asyncio.wait_for(fast.outgoing.get(), 1))["event_id"] == str(first.event_id)
        await manager.publish(second)
        assert (await asyncio.wait_for(fast.outgoing.get(), 1))["event_id"] == str(second.event_id)
        await manager.publish(third)
        assert (await asyncio.wait_for(fast.outgoing.get(), 1))["event_id"] == str(third.event_id)
        await asyncio.wait_for(slow.closed.wait(), timeout=1)
        assert slow.close_code == 1013
        assert manager.connection_count == 1
    finally:
        await manager.close_all()
        await asyncio.gather(*tasks)


async def test_send_timeout_closes_only_that_connection():
    manager = ConnectionManager(FakeAuth(), send_timeout=0.02)
    websocket = FakeSocket(block_samples=True)
    task = await connect(manager, websocket)
    await manager.publish(event())
    await asyncio.wait_for(task, timeout=1)
    assert websocket.close_code == 1013
    assert manager.connection_count == 0


@pytest.mark.parametrize("database_down,expected", [(False, 1008), (True, 1011)])
async def test_idle_connections_revalidate_authentication(database_down, expected):
    auth = FakeAuth()
    manager = ConnectionManager(auth, auth_check_interval=0.01)
    websocket = FakeSocket()
    task = await connect(manager, websocket)
    auth.revoked = not database_down
    auth.database_down = database_down
    await asyncio.wait_for(task, timeout=1)
    assert websocket.close_code == expected


async def test_revocation_is_checked_before_queued_event_is_sent():
    auth = FakeAuth()
    manager = ConnectionManager(auth, auth_check_interval=10)
    websocket = FakeSocket()
    task = await connect(manager, websocket)
    auth.revoked = True
    await manager.publish(event())
    await asyncio.wait_for(task, timeout=1)
    assert websocket.close_code == 1008
    assert websocket.outgoing.empty()


async def test_category_filter_and_ping_share_the_single_sender():
    manager = ConnectionManager(FakeAuth())
    websocket = FakeSocket()
    task = await connect(manager, websocket, category="humidity")
    try:
        await manager.publish(event("temperature"))
        websocket.incoming.put_nowait({"type": "websocket.receive", "text": '{"type":"ping"}'})
        assert await asyncio.wait_for(websocket.outgoing.get(), 1) == {"type": "pong"}
        expected = event("humidity")
        await manager.publish(expected)
        assert (await asyncio.wait_for(websocket.outgoing.get(), 1))["event_id"] == str(
            expected.event_id
        )
    finally:
        await manager.close_all()
        await task


async def test_device_filter_requires_both_device_and_category_to_match():
    manager = ConnectionManager(FakeAuth())
    selected, all_devices = FakeSocket(), FakeSocket()
    tasks = [
        await connect(manager, selected, category="temperature", device_id="device-002"),
        await connect(manager, all_devices),
    ]
    try:
        excluded = [
            event("temperature", device_id="device-001"),
            event("humidity", device_id="device-002"),
            event("temperature"),
        ]
        for sample in excluded:
            await manager.publish(sample)
            assert (await asyncio.wait_for(all_devices.outgoing.get(), 1))["event_id"] == str(
                sample.event_id
            )
        selected.incoming.put_nowait({"type": "websocket.receive", "text": '{"type":"ping"}'})
        assert await asyncio.wait_for(selected.outgoing.get(), 1) == {"type": "pong"}
        expected = event("temperature", device_id="device-002")
        await manager.publish(expected)
        assert await asyncio.wait_for(selected.outgoing.get(), 1) == expected.model_dump(
            mode="json"
        )
        assert await asyncio.wait_for(all_devices.outgoing.get(), 1) == expected.model_dump(
            mode="json"
        )
    finally:
        await manager.close_all()
        await asyncio.gather(*tasks)


@pytest.mark.parametrize(
    "message",
    [
        {"type": "websocket.receive", "bytes": b"binary"},
        {"type": "websocket.receive", "text": "not-json"},
        {"type": "websocket.receive", "text": '{"type":"sample.created"}'},
        {"type": "websocket.receive", "text": "x" * 4097},
    ],
)
async def test_invalid_control_messages_are_closed(message):
    manager = ConnectionManager(FakeAuth())
    websocket = FakeSocket()
    task = await connect(manager, websocket)
    websocket.incoming.put_nowait(message)
    await asyncio.wait_for(task, timeout=1)
    assert websocket.close_code == 1008
    assert manager.connection_count == 0


async def test_disconnect_removes_connection_and_background_tasks():
    manager = ConnectionManager(FakeAuth())
    websocket = FakeSocket()
    task = await connect(manager, websocket)
    websocket.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(task, timeout=1)
    assert manager.connection_count == 0


async def test_connection_limit_and_shutdown_are_bounded():
    manager = ConnectionManager(FakeAuth(), max_connections=1, send_timeout=0.02)
    first, second = FakeSocket(block_close=True), FakeSocket()
    task = await connect(manager, first)
    await manager.serve(second, "token", principal())
    assert not second.accepted
    assert second.close_code == 1013
    await asyncio.wait_for(manager.close_all(), timeout=1)
    await task
    assert first.close_code == 1001
    assert manager.connection_count == 0


async def test_zero_connection_limit_accepts_more_than_default_and_broadcasts():
    manager = ConnectionManager(FakeAuth(), max_connections=0)
    sockets = [FakeSocket() for _ in range(128)]
    tasks = []
    try:
        for websocket in sockets:
            tasks.append(await connect(manager, websocket))
        assert manager.connection_count == len(sockets)
        assert all(websocket.accepted for websocket in sockets)
        sample = event()
        await manager.publish(sample)
        messages = await asyncio.wait_for(
            asyncio.gather(*(websocket.outgoing.get() for websocket in sockets)), timeout=2
        )
        assert all(message["event_id"] == str(sample.event_id) for message in messages)
    finally:
        await manager.close_all()
        await asyncio.gather(*tasks)
    assert manager.connection_count == 0
    assert all(websocket.close_code == 1001 for websocket in sockets)


def test_negative_connection_limit_is_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        ConnectionManager(FakeAuth(), max_connections=-1)


async def test_initially_disabled_rejects_connections_and_can_be_enabled():
    manager = ConnectionManager(FakeAuth(), enabled=False)
    rejected = FakeSocket()
    await manager.serve(rejected, "token", principal())
    assert not manager.enabled
    assert not rejected.accepted
    assert rejected.close_code == 1001
    assert rejected.close_reason == "WebSocket service disabled"
    assert manager.connection_count == 0

    await manager.set_enabled(True)
    connected = FakeSocket()
    task = await connect(manager, connected)
    try:
        assert manager.enabled
        assert manager.connection_count == 1
    finally:
        await manager.close_all()
        await task


@pytest.mark.parametrize("shutdown", [False, True])
@pytest.mark.parametrize("denial_supported", [False, True])
async def test_unavailable_handshake_uses_503_with_supported_transport(shutdown, denial_supported):
    manager = ConnectionManager(FakeAuth(), enabled=False)
    if shutdown:
        await manager.close_all()
    messages = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        messages.append(message)

    websocket = WebSocket(
        {
            "type": "websocket",
            "extensions": {"websocket.http.response": {}} if denial_supported else {},
        },
        receive=receive,
        send=send,
    )
    await manager.serve(websocket, "token", principal())
    reason = "Server shutting down" if shutdown else "WebSocket service disabled"
    if denial_supported:
        assert messages[0]["type"] == "websocket.http.response.start"
        assert messages[0]["status"] == 503
        assert (b"retry-after", b"1") in messages[0]["headers"]
        assert messages[1]["type"] == "websocket.http.response.body"
        assert json.loads(messages[1]["body"]) == {"detail": reason}
    else:
        assert messages == [{"type": "websocket.close", "code": 1001, "reason": reason}]
    assert manager.connection_count == 0


async def test_unavailable_handshake_denial_timeout_falls_back_to_bounded_close():
    class StalledDenialSocket(FakeSocket):
        scope = {"extensions": {"websocket.http.response": {}}}

        async def send_denial_response(self, response):
            await asyncio.Event().wait()

    manager = ConnectionManager(FakeAuth(), enabled=False, send_timeout=0.01)
    websocket = StalledDenialSocket()
    await asyncio.wait_for(manager.serve(websocket, "token", principal()), 1)
    assert not websocket.accepted
    assert websocket.close_code == 1001
    assert websocket.close_reason == "WebSocket service disabled"


async def test_disabling_closes_clients_rejects_new_clients_and_allows_reconnect():
    manager = ConnectionManager(FakeAuth())
    first, second = FakeSocket(), FakeSocket()
    tasks = [await connect(manager, first), await connect(manager, second)]
    await manager.set_enabled(False)
    await asyncio.gather(*tasks)
    assert not manager.enabled
    assert manager.connection_count == 0
    for websocket in (first, second):
        assert websocket.close_code == 1001
        assert websocket.close_reason == "WebSocket service disabled"

    rejected = FakeSocket()
    await manager.serve(rejected, "token", principal())
    assert not rejected.accepted
    assert rejected.close_code == 1001
    await manager.set_enabled(False)
    await manager.set_enabled(True)
    await manager.set_enabled(True)
    reconnected = FakeSocket()
    task = await connect(manager, reconnected)
    try:
        sample = event()
        await manager.publish(sample)
        assert await asyncio.wait_for(reconnected.outgoing.get(), 1) == sample.model_dump(
            mode="json"
        )
    finally:
        await manager.close_all()
        await task


async def test_disable_during_handshake_never_sends_ready_or_accepts_new_connections():
    manager = ConnectionManager(FakeAuth())
    pending = FakeSocket(block_accept=True)
    serve_task = asyncio.create_task(manager.serve(pending, "token", principal()))
    await asyncio.wait_for(pending.accept_started.wait(), 1)
    disable_task = asyncio.create_task(manager.set_enabled(False))
    await asyncio.sleep(0)
    assert not manager.enabled
    rejected = FakeSocket()
    await manager.serve(rejected, "token", principal())
    assert not rejected.accepted
    assert rejected.close_code == 1001

    pending.accept_release.set()
    await asyncio.wait_for(asyncio.gather(disable_task, serve_task), 1)
    assert pending.outgoing.empty()
    assert pending.close_code == 1001
    assert pending.close_reason == "WebSocket service disabled"
    assert manager.connection_count == 0


async def test_enable_waits_for_concurrent_disable_to_finish_closing_clients():
    manager = ConnectionManager(FakeAuth())
    websocket = FakeSocket(block_close=True)
    serve_task = await connect(manager, websocket)
    disable_task = asyncio.create_task(manager.set_enabled(False))
    await asyncio.wait_for(websocket.closed.wait(), 1)
    enable_task = asyncio.create_task(manager.set_enabled(True))
    await asyncio.sleep(0)
    assert not manager.enabled
    assert not enable_task.done()
    websocket.close_release.set()
    await asyncio.wait_for(asyncio.gather(disable_task, enable_task, serve_task), 1)
    assert manager.enabled
    assert manager.connection_count == 0


async def test_shutdown_during_disable_cannot_be_undone_by_queued_or_later_enable():
    manager = ConnectionManager(FakeAuth())
    websocket = FakeSocket(block_close=True)
    serve_task = await connect(manager, websocket)
    disable_task = asyncio.create_task(manager.set_enabled(False))
    await asyncio.wait_for(websocket.closed.wait(), 1)
    enable_task = asyncio.create_task(manager.set_enabled(True))
    shutdown_task = asyncio.create_task(manager.close_all())
    await asyncio.sleep(0)
    assert not manager.enabled
    websocket.close_release.set()
    await asyncio.wait_for(asyncio.gather(disable_task, enable_task, shutdown_task, serve_task), 1)
    await manager.set_enabled(True)
    await manager.close_all()
    assert not manager.enabled
    rejected = FakeSocket()
    await manager.serve(rejected, "token", principal())
    assert not rejected.accepted
    assert rejected.close_code == 1001
    assert rejected.close_reason == "Server shutting down"
