"""Device monitoring reads committed MariaDB samples for every authenticated role."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.realtime import RealtimeSample
from app.services.monitoring import create_sample_event

pytestmark = pytest.mark.integration

DEVICE_NAMES = {"device-001": "設備 A", "device-002": "設備 B", "device-003": "設備 C"}


def sample(*, device_id="device-001", value="25", **kwargs):
    return create_sample_event(
        value=Decimal(value),
        device_id=device_id,
        device_name=DEVICE_NAMES[device_id],
        unit="°C",
        **kwargs,
    )


async def persist(app, *events):
    for event in events:
        await app.state.event_dispatcher.publish(event)
    assert await app.state.batch_writer.stop(timeout=3)


async def test_device_catalog_and_recent_samples_require_login(client):
    for path in ("devices", "samples"):
        assert (await client.get(f"/api/v1/monitoring/{path}")).status_code == 401


@pytest.mark.parametrize("role", ["viewer", "user", "admin"])
async def test_device_catalog_and_samples_are_available_to_all_roles(
    client, app, make_account, role
):
    account = await make_account(f"devices-{role}@example.com", role=role)
    event = sample(device_id="device-002", value="85.125")
    await persist(app, event)
    response = await client.get("/api/v1/monitoring/devices", headers=account.headers)
    assert response.status_code == 200, response.text
    assert (
        response.json()["sample_interval_seconds"] == app.state.settings.simulator_interval_seconds
    )
    devices = {item["id"]: item for item in response.json()["items"]}
    assert set(devices) == {*DEVICE_NAMES, "legacy"}
    for device_id, name in DEVICE_NAMES.items():
        device = devices[device_id]
        assert device["name"] == name
        assert device["category"] == "temperature"
        assert device["unit"] == "°C"
        assert Decimal(device["threshold"]) == app.state.settings.simulator_threshold
    assert devices["legacy"] == {
        "id": "legacy",
        "name": "舊版未指定設備",
        "category": None,
        "unit": "",
        "threshold": None,
    }

    response = await client.get("/api/v1/monitoring/samples", headers=account.headers)
    assert response.status_code == 200, response.text
    page = response.json()
    assert (page["total"], page["page"], page["page_size"]) == (1, 1, 300)
    saved = page["items"][0]
    assert saved["event_id"] == str(event.event_id)
    assert (saved["device_id"], saved["device_name"], saved["unit"]) == (
        "device-002",
        "設備 B",
        "°C",
    )
    assert saved["is_anomaly"] is True
    assert "persistence" not in saved
    assert "password_hash" not in saved and "token_version" not in saved
    if role != "admin":
        for path in ("history", "status"):
            response = await client.get(f"/api/v1/monitoring/{path}", headers=account.headers)
            assert response.status_code == 403


async def test_samples_show_only_committed_rows(client, app, make_account):
    account = await make_account("samples-pending@example.com", role="viewer")
    event = sample()
    await app.state.event_dispatcher.publish(event)
    response = await client.get("/api/v1/monitoring/samples", headers=account.headers)
    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert await app.state.batch_writer.stop(timeout=3)
    response = await client.get("/api/v1/monitoring/samples", headers=account.headers)
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["event_id"] == str(event.event_id)


async def test_samples_device_category_time_anomaly_filters_and_stable_pagination(
    client, app, make_account
):
    account = await make_account("samples-filters@example.com", role="viewer")
    start = datetime(2026, 11, 8, tzinfo=UTC)
    end = start + timedelta(hours=1)
    included = [sample(value=str(value), timestamp=start) for value in (81, 82, 83)]
    excluded = [
        sample(device_id="device-002", value="99", timestamp=start),
        sample(category="humidity", value="99", timestamp=start),
        sample(value="99", timestamp=start - timedelta(microseconds=1)),
        sample(value="99", timestamp=end),
        sample(value="80", timestamp=start),
    ]
    await persist(app, *included, *excluded)
    params = {
        "device_id": "device-001",
        "category": "temperature",
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "is_anomaly": "true",
        "page_size": 2,
    }
    first = await client.get("/api/v1/monitoring/samples", params=params, headers=account.headers)
    second = await client.get(
        "/api/v1/monitoring/samples", params={**params, "page": 2}, headers=account.headers
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["total"] == second.json()["total"] == 3
    combined = first.json()["items"] + second.json()["items"]
    assert [row["event_id"] for row in combined] == [str(e.event_id) for e in reversed(included)]
    by_event = await client.get(
        "/api/v1/monitoring/samples",
        params={"event_id": str(included[0].event_id), "device_id": "device-001"},
        headers=account.headers,
    )
    assert by_event.status_code == 200
    assert by_event.json()["total"] == 1
    assert by_event.json()["items"][0]["event_id"] == str(included[0].event_id)
    for device_id in ("device-003", "unknown-device"):
        response = await client.get(
            "/api/v1/monitoring/samples", params={"device_id": device_id}, headers=account.headers
        )
        assert response.status_code == 200
        assert response.json()["total"] == 0


async def test_recent_samples_window_allows_300_and_admin_history_keeps_100_limit(
    client, app, make_account
):
    account = await make_account("samples-window@example.com", role="admin")
    timestamp = datetime(2026, 11, 8, tzinfo=UTC)
    events = [sample(timestamp=timestamp, value=str(index)) for index in range(305)]
    await persist(app, *events)
    response = await client.get("/api/v1/monitoring/samples", headers=account.headers)
    assert response.status_code == 200
    page = response.json()
    assert (page["total"], page["page_size"], len(page["items"])) == (305, 300, 300)
    assert [item["event_id"] for item in page["items"]] == [
        str(event.event_id) for event in reversed(events[5:])
    ]
    response = await client.get(
        "/api/v1/monitoring/samples", params={"page": 2, "page_size": 300}, headers=account.headers
    )
    assert response.status_code == 200
    assert [item["event_id"] for item in response.json()["items"]] == [
        str(event.event_id) for event in reversed(events[:5])
    ]
    history = await client.get(
        "/api/v1/monitoring/history", params={"page_size": 101}, headers=account.headers
    )
    assert history.status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page_size": 0},
        {"page_size": 301},
        {"device_id": ""},
        {"device_id": "a" * 65},
        {"device_id": "device 001"},
        {"device_id": "device/001"},
        {"category": ""},
        {"q": ""},
        {"q": "x" * 201},
        {"event_id": "not-a-uuid"},
        {"is_anomaly": "invalid"},
        {"start_time": "not-a-time"},
        {"start_time": "0001-01-01T00:00:00+14:00"},
        {"start_time": "2026-11-08T00:00:00Z", "end_time": "2026-11-08T00:00:00Z"},
        {"start_time": "2026-11-09T00:00:00Z", "end_time": "2026-11-08T00:00:00Z"},
        {"unrecognized_filter": "value"},
    ],
)
async def test_samples_reject_invalid_filters(client, make_account, params):
    account = await make_account("samples-invalid@example.com")
    response = await client.get(
        "/api/v1/monitoring/samples", params=params, headers=account.headers
    )
    assert response.status_code == 422, response.text


async def test_old_sample_shape_remains_readable_as_legacy_and_can_be_filtered(
    client, app, make_account
):
    account = await make_account("legacy-samples@example.com", role="admin")
    legacy_id = str(uuid4())
    # Insert through the pre-device ORM constructor shape: defaults preserve old producers.
    async with app.state.session_factory.begin() as session:
        session.add(
            RealtimeSample(
                event_id=legacy_id,
                title="pre-device sample",
                value=Decimal("20"),
                category="temperature",
                timestamp=datetime(2026, 11, 8, tzinfo=UTC),
                threshold=Decimal("80"),
                is_anomaly=False,
            )
        )
    factory_legacy = create_sample_event(value=Decimal("21"))
    await persist(app, factory_legacy, sample())
    for path in ("samples", "history"):
        response = await client.get(
            f"/api/v1/monitoring/{path}", params={"device_id": "legacy"}, headers=account.headers
        )
        assert response.status_code == 200
        assert response.json()["total"] == 2
        assert {row["event_id"] for row in response.json()["items"]} == {
            legacy_id,
            str(factory_legacy.event_id),
        }
        assert all(
            (row["device_id"], row["device_name"], row["unit"]) == ("legacy", "舊版未指定設備", "")
            for row in response.json()["items"]
        )
        response = await client.get(
            f"/api/v1/monitoring/{path}",
            params={"device_id": "device-001"},
            headers=account.headers,
        )
        assert response.status_code == 200
        assert response.json()["total"] == 1
        assert response.json()["items"][0]["device_name"] == "設備 A"
