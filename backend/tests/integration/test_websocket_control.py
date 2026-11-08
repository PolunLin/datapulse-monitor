"""Admin stream controls use real WebSocket transport without stopping HTTP or persistence."""

import asyncio
import json
import socket

import pytest
import pytest_asyncio
import uvicorn
from httpx import ASGITransport, AsyncClient
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.main import create_app
from app.realtime.generator import Simulator

pytestmark = pytest.mark.integration
CONTROL_URL = "/api/v1/monitoring/websocket"


@pytest_asyncio.fixture
async def stream_url(app):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"ws://127.0.0.1:{listener.getsockname()[1]}/ws/v1/samples"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        finally:
            listener.close()


async def receive(websocket):
    return json.loads(await asyncio.wait_for(websocket.recv(), timeout=3))


@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_stream_controls_require_authentication(client, method):
    for headers in ({}, {"Authorization": "Bearer invalid"}):
        body = {"json": {"enabled": False}} if method == "PUT" else {}
        response = await client.request(method, CONTROL_URL, headers=headers, **body)
        assert response.status_code == 401


@pytest.mark.parametrize("role", ["user", "viewer"])
async def test_stream_controls_require_admin(client, app, make_account, role):
    account = await make_account(f"ws-control-{role}@example.com", role=role)
    assert (await client.get(CONTROL_URL, headers=account.headers)).status_code == 403
    response = await client.put(CONTROL_URL, headers=account.headers, json={"enabled": False})
    assert response.status_code == 403
    assert app.state.ws_manager.enabled is True


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"enabled": None},
        {"enabled": "false"},
        {"enabled": 0},
        {"enabled": False, "extra": True},
    ],
)
async def test_stream_control_rejects_invalid_payload_without_changing_state(
    client, app, make_account, payload
):
    admin = await make_account("ws-control-validation@example.com", role="admin")
    response = await client.put(CONTROL_URL, headers=admin.headers, json=payload)
    assert response.status_code == 422
    assert app.state.ws_manager.enabled is True


async def test_stop_closes_all_sockets_rejects_new_connections_and_start_allows_reconnect(
    stream_url, client, app, make_account
):
    admin = await make_account("ws-control-admin@example.com", role="admin")
    viewer = await make_account("ws-control-subscriber@example.com", role="viewer")
    response = await client.get(CONTROL_URL, headers=admin.headers)
    assert response.status_code == 200
    assert response.json() == {"enabled": True, "connections": 0}

    async with (
        connect(stream_url, additional_headers=admin.headers, proxy=None) as first,
        connect(stream_url, additional_headers=viewer.headers, proxy=None) as second,
    ):
        assert (await receive(first))["type"] == "ready"
        assert (await receive(second))["type"] == "ready"
        assert (await client.get(CONTROL_URL, headers=admin.headers)).json() == {
            "enabled": True,
            "connections": 2,
        }
        for _ in range(2):
            response = await client.put(CONTROL_URL, headers=admin.headers, json={"enabled": False})
            assert response.status_code == 200
            assert response.json() == {"enabled": False, "connections": 0}
        for websocket in (first, second):
            with pytest.raises(ConnectionClosed) as exc:
                await receive(websocket)
            assert exc.value.rcvd.code == 1001

    for headers in (viewer.headers, {}):
        with pytest.raises(InvalidStatus) as exc:
            async with connect(stream_url, additional_headers=headers, proxy=None):
                pytest.fail("Disabled WebSocket accepted a new connection")
        # A temporary stream outage must remain retryable for the frontend receiver;
        # 401/403 would instead clear the user's authenticated UI session.
        assert exc.value.response.status_code == 503
    status = await client.get("/api/v1/monitoring/status", headers=admin.headers)
    assert status.status_code == 200
    assert status.json()["websocket_enabled"] is False

    for _ in range(2):
        response = await client.put(CONTROL_URL, headers=admin.headers, json={"enabled": True})
        assert response.status_code == 200
        assert response.json() == {"enabled": True, "connections": 0}
    async with connect(stream_url, additional_headers=viewer.headers, proxy=None) as websocket:
        assert (await receive(websocket))["type"] == "ready"
        await websocket.send('{"type":"ping"}')
        assert await receive(websocket) == {"type": "pong"}
    assert app.state.ws_manager.enabled is True


async def test_disabled_stream_keeps_rest_crud_auth_and_simulator_persistence_running(
    app, client, make_account, record_payload
):
    admin = await make_account("ws-control-rest@example.com", role="admin")
    writer = app.state.batch_writer
    writer.flush_interval = 0.03
    simulator = Simulator(app.state.event_dispatcher, interval=0.02, random_seed=17)
    app.state.simulator = simulator
    simulator.start()
    writer_task, simulator_task = writer.task, simulator.task
    try:
        response = await client.put(CONTROL_URL, headers=admin.headers, json={"enabled": False})
        assert response.status_code == 200
        generated_before, persisted_before = simulator.generated_count, writer.persisted_count
        async with asyncio.timeout(3):
            while (
                simulator.generated_count < generated_before + 6
                or writer.persisted_count < persisted_before + 6
            ):
                await asyncio.sleep(0.01)
        assert writer.task is writer_task and not writer.task.done()
        assert simulator.task is simulator_task and not simulator.task.done()
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 200
        login = await client.post(
            "/api/v1/auth/login", data={"username": admin.email, "password": admin.password}
        )
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        assert (await client.get("/api/v1/users/me", headers=headers)).status_code == 200
        history = await client.get("/api/v1/monitoring/samples", headers=headers)
        assert history.status_code == 200
        assert history.json()["total"] >= persisted_before + 6

        created = await client.post("/api/v1/records", headers=headers, json=record_payload)
        assert created.status_code == 201
        record_url = f"/api/v1/records/{created.json()['id']}"
        assert (await client.get(record_url, headers=headers)).status_code == 200
        changed = await client.patch(record_url, headers=headers, json={"title": "WS stopped"})
        assert changed.status_code == 200
        assert changed.json()["title"] == "WS stopped"
        assert (await client.delete(record_url, headers=headers)).status_code == 204
        assert (await client.get(CONTROL_URL, headers=headers)).json() == {
            "enabled": False,
            "connections": 0,
        }
    finally:
        await simulator.stop()


async def test_lifespan_honors_disabled_startup_setting_and_keeps_rest_ready(settings):
    application = create_app(settings.model_copy(update={"ws_enabled": False}))
    async with application.router.lifespan_context(application):
        assert application.state.ws_manager.enabled is False
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://testserver"
        ) as client:
            assert (await client.get("/health/ready")).status_code == 200
        await application.state.ws_manager.set_enabled(True)
        assert application.state.ws_manager.enabled is True
