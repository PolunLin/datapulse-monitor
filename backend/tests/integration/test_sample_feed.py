"""Cursor progress follows committed insertion IDs, not measurement timestamps."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.repositories.realtime import add_missing_samples
from app.services.monitoring import create_sample_event

pytestmark = pytest.mark.integration
FEED_PATH = "/api/v1/monitoring/sample-feed"


def sample(*, device_id="device-001", timestamp=None):
    return create_sample_event(value=Decimal("25.123456"), device_id=device_id, timestamp=timestamp)


async def save(app, *events):
    # Separate short commits reproduce the serial batch writer's ordering.
    async with app.state.session_factory.begin() as session:
        await add_missing_samples(session, events)


async def read_feed(client, account, **params):
    response = await client.get(
        FEED_PATH, headers=account.headers, params={"device_id": "device-001", **params}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items", "next_cursor", "has_more"}
    return body


async def test_feed_requires_login(client):
    response = await client.get(FEED_PATH, params={"device_id": "device-001"})
    assert response.status_code == 401


async def test_device_catalog_reports_configured_sampling_interval(client, app, make_account):
    account = await make_account("feed-interval@example.com")
    app.state.settings.simulator_interval_seconds = 30.0
    response = await client.get("/api/v1/monitoring/devices", headers=account.headers)
    assert response.status_code == 200
    assert response.json()["sample_interval_seconds"] == 30.0


@pytest.mark.parametrize("role", ["viewer", "user", "admin"])
async def test_feed_is_available_to_all_authenticated_roles(client, app, make_account, role):
    account = await make_account(f"feed-{role}@example.com", role=role)
    event = sample()
    await save(app, event)
    body = await read_feed(client, account)
    assert [row["event_id"] for row in body["items"]] == [str(event.event_id)]
    assert body["next_cursor"] == body["items"][-1]["id"]
    assert body["has_more"] is False
    assert body["items"][0]["value"] == "25.123456"


async def test_snapshot_reads_latest_commits_with_bounded_device_scope(client, app, make_account):
    account = await make_account("feed-snapshot@example.com")
    now = datetime(2026, 11, 8, tzinfo=UTC)
    # Reverse event times deliberately: the initial tail follows insertion order.
    events = [sample(timestamp=now - timedelta(seconds=i)) for i in range(5)]
    await save(app, *events, sample(device_id="device-002"))
    body = await read_feed(client, account, limit=3)
    assert [row["event_id"] for row in body["items"]] == [
        str(event.event_id) for event in events[-3:]
    ]
    assert body["next_cursor"] == body["items"][-1]["id"]
    assert body["has_more"] is False  # Older history belongs to the paginated API.


async def test_incremental_pages_follow_cursor_without_duplicates_and_keep_empty_cursor(
    client, app, make_account
):
    account = await make_account("feed-pages@example.com")
    events = [sample() for _ in range(5)]
    await save(app, *events[:2], sample(device_id="device-002"), *events[2:])
    first = await read_feed(client, account, after_id=0, limit=2)
    second = await read_feed(client, account, after_id=first["next_cursor"], limit=2)
    third = await read_feed(client, account, after_id=second["next_cursor"], limit=2)
    empty = await read_feed(client, account, after_id=third["next_cursor"], limit=2)
    combined = first["items"] + second["items"] + third["items"]
    assert [row["event_id"] for row in combined] == [str(event.event_id) for event in events]
    assert [first["has_more"], second["has_more"], third["has_more"]] == [True, True, False]
    assert empty == {"items": [], "next_cursor": third["next_cursor"], "has_more": False}


async def test_delayed_older_measurement_is_returned_after_snapshot_cursor(
    client, app, make_account
):
    account = await make_account("feed-delayed@example.com")
    now = datetime(2026, 11, 8, tzinfo=UTC)
    initial = sample(timestamp=now)
    await save(app, initial)
    snapshot = await read_feed(client, account)
    delayed = sample(timestamp=now - timedelta(days=1))
    await save(app, delayed)
    body = await read_feed(client, account, after_id=snapshot["next_cursor"])
    assert [row["event_id"] for row in body["items"]] == [str(delayed.event_id)]
    assert body["next_cursor"] > snapshot["next_cursor"]


async def test_empty_snapshot_can_later_observe_a_commit(client, app, make_account):
    account = await make_account("feed-empty@example.com")
    snapshot = await read_feed(client, account)
    assert snapshot == {"items": [], "next_cursor": 0, "has_more": False}
    event = sample()
    await save(app, event)
    delta = await read_feed(client, account, after_id=snapshot["next_cursor"])
    assert [row["event_id"] for row in delta["items"]] == [str(event.event_id)]
    unknown = await read_feed(client, account, device_id="unknown-device", after_id=123)
    assert unknown == {"items": [], "next_cursor": 123, "has_more": False}


async def test_pending_measurements_do_not_advance_feed_cursor(client, app, make_account):
    account = await make_account("feed-pending@example.com")
    event = sample()
    await app.state.event_dispatcher.publish(event)
    pending = await read_feed(client, account)
    assert pending == {"items": [], "next_cursor": 0, "has_more": False}
    assert await app.state.batch_writer.stop(timeout=3)
    committed = await read_feed(client, account, after_id=pending["next_cursor"])
    assert [row["event_id"] for row in committed["items"]] == [str(event.event_id)]


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"device_id": ""},
        {"device_id": "invalid device"},
        {"device_id": "a" * 65},
        {"device_id": "device-001", "after_id": -1},
        {"device_id": "device-001", "after_id": "invalid"},
        {"device_id": "device-001", "limit": 0},
        {"device_id": "device-001", "limit": 301},
        {"device_id": "device-001", "page": 1},
    ],
)
async def test_feed_validates_device_cursor_limit_and_unknown_parameters(
    client, make_account, params
):
    account = await make_account("feed-validation@example.com")
    response = await client.get(FEED_PATH, headers=account.headers, params=params)
    assert response.status_code == 422, response.text
