"""Monitoring history has its own authorization and never exposes pending events."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.monitoring import create_sample_event

pytestmark = pytest.mark.integration


async def persist(app, *events):
    for event in events:
        await app.state.event_dispatcher.publish(event)
    # A graceful stop proves partial batches commit without waiting for the timer.
    assert await app.state.batch_writer.stop(timeout=3)


async def test_history_and_pipeline_status_are_admin_only(client, make_account):
    for path in ("history", "status"):
        assert (await client.get(f"/api/v1/monitoring/{path}")).status_code == 401
    for index, role in enumerate(("viewer", "user")):
        account = await make_account(f"monitor-reader-{index}@example.com", role=role)
        for path in ("history", "status"):
            response = await client.get(f"/api/v1/monitoring/{path}", headers=account.headers)
            assert response.status_code == 403
    admin = await make_account("monitor-admin@example.com", role="admin")
    status = await client.get("/api/v1/monitoring/status", headers=admin.headers)
    assert status.status_code == 200
    assert status.json()["writer_running"] is True
    assert status.json()["writer_healthy"] is True
    assert status.json()["generator_running"] is False
    assert status.json()["pending_count"] == 0


async def test_history_separates_pending_events_and_preserves_snapshot(client, app, make_account):
    admin = await make_account("monitor-pending@example.com", role="admin")
    event = create_sample_event(value=Decimal("85.125"))
    await app.state.event_dispatcher.publish(event)
    pending = await client.get("/api/v1/monitoring/status", headers=admin.headers)
    assert pending.json()["pending_count"] == 1
    history = await client.get("/api/v1/monitoring/history", headers=admin.headers)
    assert history.json()["total"] == 0
    assert await app.state.batch_writer.stop(timeout=3)
    history = await client.get(
        "/api/v1/monitoring/history",
        params={"event_id": str(event.event_id)},
        headers=admin.headers,
    )
    assert history.status_code == 200
    sample = history.json()["items"][0]
    assert sample["event_id"] == str(event.event_id)
    assert sample["value"] == "85.125000"
    assert sample["threshold"] == "80.000000"
    assert sample["is_anomaly"] is True
    assert "persistence" not in sample  # Only committed rows belong to this API.
    assert (await client.get("/api/v1/records", headers=admin.headers)).json()["total"] == 0


async def test_history_filters_time_window_and_stable_pagination(client, app, make_account):
    admin = await make_account("monitor-filter@example.com", role="admin")
    start = datetime(2026, 11, 8, tzinfo=UTC)
    samples = [
        create_sample_event(value=Decimal(index), category="cpu", timestamp=start)
        for index in range(3)
    ]
    end = start + timedelta(hours=1)
    excluded = create_sample_event(value=Decimal("99"), category="cpu", timestamp=end)
    other = create_sample_event(value=Decimal("1"), category="memory", timestamp=start)
    await persist(app, *samples, excluded, other)
    params = {
        "category": "cpu",
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "page_size": 2,
    }
    first = await client.get("/api/v1/monitoring/history", params=params, headers=admin.headers)
    second = await client.get(
        "/api/v1/monitoring/history", params={**params, "page": 2}, headers=admin.headers
    )
    assert first.json()["total"] == second.json()["total"] == 3
    combined = first.json()["items"] + second.json()["items"]
    assert [item["event_id"] for item in combined] == [str(s.event_id) for s in reversed(samples)]
    for params in (
        {"event_id": "not-a-uuid"},
        {"page_size": 101},
        {"start_time": end.isoformat(), "end_time": start.isoformat()},
        {"start_time": "0001-01-01T00:00:00+14:00"},
    ):
        response = await client.get(
            "/api/v1/monitoring/history", params=params, headers=admin.headers
        )
        assert response.status_code == 422


async def test_readiness_detects_stopped_writer_but_liveness_stays_ok(app, client):
    assert (await client.get("/health/ready")).status_code == 200
    assert await app.state.batch_writer.stop(timeout=1)
    assert (await client.get("/health/ready")).status_code == 503
    assert (await client.get("/health/live")).status_code == 200


async def test_history_can_filter_anomaly_state_including_threshold_boundary(
    client, app, make_account
):
    admin = await make_account("monitor-anomaly-filter@example.com", role="admin")
    await persist(
        app,
        create_sample_event(value=Decimal("80")),
        create_sample_event(value=Decimal("80.000001")),
        create_sample_event(value=Decimal("1")),
    )
    for flag, expected_count in (("true", 1), ("false", 2)):
        response = await client.get(
            "/api/v1/monitoring/history", params={"is_anomaly": flag}, headers=admin.headers
        )
        assert response.status_code == 200
        assert response.json()["total"] == expected_count
        assert all(item["is_anomaly"] == (flag == "true") for item in response.json()["items"])
    response = await client.get(
        "/api/v1/monitoring/history", params={"is_anomaly": "invalid"}, headers=admin.headers
    )
    assert response.status_code == 422
