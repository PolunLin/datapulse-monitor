"""Real Uvicorn/WebSocket transport with JWT accounts stored in the test MariaDB."""

import asyncio
import json
import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import jwt
import pytest
import pytest_asyncio
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.models.user import User
from app.realtime.events import SampleEvent
from app.realtime.generator import Simulator

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def websocket_url(app):
    # Reuse the existing fixture's app and loop; never share an Engine across TestClient threads.
    app.state.ws_manager.auth_check_interval = 0.02
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"ws://127.0.0.1:{port}/ws/v1/samples"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        finally:
            listener.close()


def sample(category="temperature", *, device_id="legacy", device_name="舊版未指定設備", unit=""):
    return SampleEvent(
        title="live sample",
        value=Decimal("85.125000"),
        category=category,
        device_id=device_id,
        device_name=device_name,
        unit=unit,
        threshold=Decimal("80"),
        is_anomaly=True,
    )


async def receive(websocket) -> dict:
    return json.loads(await asyncio.wait_for(websocket.recv(), timeout=3))


async def test_websocket_requires_authorization_header(websocket_url, make_account):
    account = await make_account("ws-header@example.com")
    for url, headers in [
        (websocket_url, {}),
        (websocket_url, {"Authorization": "Bearer invalid"}),
        (f"{websocket_url}?token={account.token}", {}),
    ]:
        with pytest.raises(InvalidStatus) as exc:
            async with connect(url, additional_headers=headers, proxy=None):
                pytest.fail("Unauthenticated connection was accepted")
        assert exc.value.response.status_code == 403


async def test_viewer_and_user_receive_same_event_without_leaking_credentials(
    websocket_url, app, make_account
):
    viewer = await make_account("ws-viewer@example.com", role="viewer")
    user = await make_account("ws-user@example.com")
    async with (
        connect(websocket_url, additional_headers=viewer.headers, proxy=None) as first,
        connect(websocket_url, additional_headers=user.headers, proxy=None) as second,
    ):
        assert (await receive(first))["type"] == "ready"
        assert (await receive(second))["type"] == "ready"
        event = sample()
        await app.state.ws_manager.publish(event)
        first_event, second_event = await receive(first), await receive(second)
        assert first_event == second_event == event.model_dump(mode="json")
        assert first_event["is_anomaly"] is True
        assert first_event["persistence"] == "pending"
        assert viewer.token not in json.dumps(first_event)
        assert "password" not in json.dumps(first_event)
    async with asyncio.timeout(1):
        while app.state.ws_manager.connection_count:
            await asyncio.sleep(0.01)


async def test_category_filter_ping_and_invalid_message(websocket_url, app, make_account):
    user = await make_account("ws-filter@example.com")
    async with connect(
        f"{websocket_url}?category=humidity", additional_headers=user.headers, proxy=None
    ) as websocket:
        assert (await receive(websocket))["category"] == "humidity"
        await app.state.ws_manager.publish(sample("temperature"))
        await websocket.send('{"type":"ping"}')
        assert await receive(websocket) == {"type": "pong"}
        await app.state.ws_manager.publish(sample("humidity"))
        assert (await receive(websocket))["category"] == "humidity"
        await websocket.send('{"type":"sample.created","value":100}')
        with pytest.raises(ConnectionClosed) as exc:
            await receive(websocket)
        assert exc.value.rcvd.code == 1008


async def test_device_and_category_filters_apply_together(websocket_url, app, make_account):
    viewer = await make_account("ws-device-filter@example.com", role="viewer")
    async with connect(
        f"{websocket_url}?device_id=device-002&category=temperature",
        additional_headers=viewer.headers,
        proxy=None,
    ) as websocket:
        assert await receive(websocket) == {
            "type": "ready",
            "category": "temperature",
            "device_id": "device-002",
            "persistence": "pending",
        }
        for excluded in (
            sample(device_id="device-001", device_name="設備 A", unit="°C"),
            sample("humidity", device_id="device-002", device_name="設備 B", unit="%"),
            sample(),
        ):
            await app.state.ws_manager.publish(excluded)
        await websocket.send('{"type":"ping"}')
        assert await receive(websocket) == {"type": "pong"}
        included = sample(device_id="device-002", device_name="設備 B", unit="°C")
        await app.state.ws_manager.publish(included)
        assert await receive(websocket) == included.model_dump(mode="json")


@pytest.mark.parametrize("device_id", ["", "device%20001", "device%2F001", "x" * 65])
async def test_websocket_rejects_invalid_device_filter(websocket_url, make_account, device_id):
    user = await make_account("ws-invalid-device@example.com")
    with pytest.raises(InvalidStatus) as exc:
        async with connect(
            f"{websocket_url}?device_id={device_id}", additional_headers=user.headers, proxy=None
        ):
            pytest.fail("Invalid device filter was accepted")
    assert exc.value.response.status_code == 403


async def test_late_subscriber_can_load_committed_history_then_receive_new_samples(
    websocket_url, app, client, make_account
):
    viewer = await make_account("ws-late-subscriber@example.com", role="viewer")
    app.state.batch_writer.flush_interval = 0.03
    simulator = Simulator(app.state.event_dispatcher, interval=0.02, random_seed=11)
    simulator.start()
    try:
        async with asyncio.timeout(3):
            while app.state.batch_writer.persisted_count < 6:
                await asyncio.sleep(0.01)
        assert app.state.ws_manager.connection_count == 0
        response = await client.get("/api/v1/monitoring/samples", headers=viewer.headers)
        assert response.status_code == 200
        earlier = response.json()["items"]
        assert {row["device_id"] for row in earlier} == {"device-001", "device-002", "device-003"}
        earlier_ids = {row["event_id"] for row in earlier}
        assert len(earlier_ids) >= 6
        opened_at = datetime.now(UTC)
        async with connect(
            websocket_url, additional_headers=viewer.headers, proxy=None
        ) as websocket:
            assert (await receive(websocket))["type"] == "ready"
            history = await client.get("/api/v1/monitoring/samples", headers=viewer.headers)
            assert history.status_code == 200
            loaded = history.json()["items"]
            assert earlier_ids <= {row["event_id"] for row in loaded}
            assert all(datetime.fromisoformat(row["timestamp"]) < opened_at for row in earlier)
            live = await receive(websocket)
            assert live["type"] == "sample.created"
            assert live["event_id"] not in earlier_ids
            assert live["device_id"] in {"device-001", "device-002", "device-003"}
            assert live["unit"] == "°C"
    finally:
        await simulator.stop()
        assert await app.state.batch_writer.stop(timeout=3)


async def test_logout_closes_already_connected_idle_socket(websocket_url, client, make_account):
    user = await make_account("ws-logout@example.com")
    async with connect(websocket_url, additional_headers=user.headers, proxy=None) as websocket:
        await receive(websocket)
        assert (await client.post("/api/v1/auth/logout", headers=user.headers)).status_code == 204
        with pytest.raises(ConnectionClosed) as exc:
            await receive(websocket)
        assert exc.value.rcvd.code == 1008


async def test_disabling_account_closes_already_connected_socket(websocket_url, app, make_account):
    user = await make_account("ws-disable@example.com")
    async with connect(websocket_url, additional_headers=user.headers, proxy=None) as websocket:
        await receive(websocket)
        async with app.state.session_factory.begin() as session:
            model = await session.get(User, user.id)
            model.is_active = False
        with pytest.raises(ConnectionClosed) as exc:
            await receive(websocket)
        assert exc.value.rcvd.code == 1008


async def test_token_expiry_closes_idle_connection(websocket_url, settings, make_account):
    user = await make_account("ws-expiry@example.com")
    claims = jwt.decode(
        user.token,
        settings.jwt_secret.get_secret_value(),
        algorithms=["HS256"],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
    )
    claims["exp"] = datetime.now(UTC) + timedelta(seconds=2)
    token = jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm="HS256")
    async with connect(
        websocket_url, additional_headers={"Authorization": f"Bearer {token}"}, proxy=None
    ) as websocket:
        await receive(websocket)
        with pytest.raises(ConnectionClosed) as exc:
            await receive(websocket)
        assert exc.value.rcvd.code == 1008


async def test_generated_event_is_pushed_and_persisted_without_polluting_crud(
    websocket_url, app, client, make_account
):
    admin = await make_account("ws-complete-flow@example.com", role="admin")
    before = await client.get("/api/v1/records", headers=admin.headers)
    assert before.status_code == 200
    app.state.batch_writer.flush_interval = 0.05
    simulator = Simulator(app.state.event_dispatcher, interval=0.02, random_seed=7)

    try:
        async with connect(
            websocket_url, additional_headers=admin.headers, proxy=None
        ) as websocket:
            assert (await receive(websocket))["type"] == "ready"
            simulator.start()
            pushed = await receive(websocket)
            assert pushed["type"] == "sample.created"
            assert pushed["persistence"] == "pending"
            await simulator.stop()

            async with asyncio.timeout(3):
                while True:
                    response = await client.get(
                        "/api/v1/monitoring/history",
                        params={"event_id": pushed["event_id"]},
                        headers=admin.headers,
                    )
                    assert response.status_code == 200, response.text
                    history = response.json()
                    if history["items"]:
                        break
                    await asyncio.sleep(0.01)

            assert history["total"] == 1
            saved = history["items"][0]
            assert saved["event_id"] == pushed["event_id"]
            assert Decimal(saved["value"]) == Decimal(pushed["value"])
            assert Decimal(saved["threshold"]) == Decimal(pushed["threshold"])
            assert saved["is_anomaly"] == pushed["is_anomaly"]
            assert (saved["device_id"], saved["device_name"], saved["unit"]) == (
                pushed["device_id"],
                pushed["device_name"],
                pushed["unit"],
            )
            assert datetime.fromisoformat(saved["timestamp"]) == datetime.fromisoformat(
                pushed["timestamp"]
            )
            after = await client.get("/api/v1/records", headers=admin.headers)
            assert after.status_code == 200
            assert after.json()["total"] == before.json()["total"]
    finally:
        await simulator.stop()
