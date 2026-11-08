import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.system_log_writer import SystemLogWriter, event_from_record
from app.models.realtime import RealtimeSample
from app.models.system_log import SystemLog

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("path", ["logs", "logging-status", "database"])
async def test_admin_system_routes_reject_unauthenticated_and_non_admin(client, make_account, path):
    route = f"/api/v1/admin/{path}"
    assert (await client.get(route)).status_code == 401
    for role in ("user", "viewer"):
        user = await make_account(f"system-{role}-{path}@example.com", role)
        assert (await client.get(route, headers=user.headers)).status_code == 403


async def test_logs_query_filter_pagination_and_utc_boundaries(client, app, make_account):
    admin = await make_account("system-query@example.com", "admin")
    instant = datetime(2026, 11, 8, 1, tzinfo=UTC)
    async with app.state.session_factory.begin() as session:
        session.add_all(
            [
                SystemLog(
                    timestamp=instant + timedelta(hours=offset),
                    level=level,
                    kind="audit",
                    logger="app.admin.users",
                    message="literal %_ label",
                    request_id="test-query",
                    actor_id=admin.id,
                    target_user_id=admin.id,
                    action="test",
                    changes=None,
                )
                for offset, level in ((0, "INFO"), (0, "INFO"), (0, "WARNING"), (1, "INFO"))
            ]
        )
    filters = {
        "kind": "audit",
        "level": "INFO",
        "request_id": "test-query",
        "actor_id": admin.id,
        "start_time": "2026-11-08T09:00:00+08:00",
        "end_time": "2026-11-08T10:00:00+08:00",
        "q": "%_",
        "page_size": 1,
    }
    first = await client.get("/api/v1/admin/logs", headers=admin.headers, params=filters)
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["total"] == 2 and len(data["items"]) == 1
    assert data["items"][0]["timestamp"] == "2026-11-08T01:00:00Z"
    second = (
        await client.get("/api/v1/admin/logs", headers=admin.headers, params={**filters, "page": 2})
    ).json()
    assert second["total"] == 2
    assert data["items"][0]["id"] > second["items"][0]["id"]


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page_size": 101},
        {"kind": "password"},
        {"level": "TRACE"},
        {"actor_id": 0},
        {"start_time": "2026-11-08T00:00:00"},
        {"start_time": "2026-11-08T00:00:00Z", "end_time": "2026-11-08T00:00:00Z"},
    ],
)
async def test_log_filters_are_validated(client, make_account, params):
    admin = await make_account("system-invalid@example.com", "admin")
    assert (
        await client.get("/api/v1/admin/logs", headers=admin.headers, params=params)
    ).status_code == 422


async def test_request_logs_are_persisted_sanitized_and_keep_actor(client, app, make_account):
    admin = await make_account("system-persisted@example.com", "admin")
    response = await client.get(
        "/api/v1/records",
        headers={**admin.headers, "X-Request-ID": "safe-id-42"},
        params={"q": "private-payload"},
    )
    assert response.status_code == 200
    assert await app.state.system_log_writer.stop()
    result = await client.get(
        "/api/v1/admin/logs", headers=admin.headers, params={"request_id": "safe-id-42"}
    )
    assert result.status_code == 200
    assert result.json()["total"] == 1
    item = result.json()["items"][0]
    assert item["path"] == "/api/v1/records" and item["actor_id"] == admin.id
    assert "private-payload" not in result.text and admin.token not in result.text
    # A separate service session still sees committed logs after the writer is stopped.
    async with app.state.session_factory() as session:
        assert (
            await session.scalar(
                select(func.count(SystemLog.id)).where(SystemLog.request_id == "safe-id-42")
            )
            == 1
        )


async def test_writer_retention_and_uncertain_commit_retry_are_idempotent(app):
    await app.state.system_log_writer.stop()
    writer = SystemLogWriter(app.state.session_factory, retained_limit=3)
    records = [
        event_from_record(
            logging.LogRecord(
                "app.lifecycle", logging.INFO, "", 0, "Application resources initialized", (), None
            )
        )
        for _ in range(5)
    ]
    await writer._persist(records)
    await writer._persist(records[-3:])
    async with app.state.session_factory() as session:
        stored = list(
            await session.scalars(select(SystemLog).order_by(SystemLog.timestamp, SystemLog.id))
        )
    assert len(stored) == 3
    assert [row.event_id for row in stored] == [row["event_id"] for row in records[-3:]]


async def test_audit_only_retention_without_runtime_events(app):
    await app.state.system_log_writer.stop()
    async with app.state.session_factory.begin() as session:
        session.add_all(
            [
                SystemLog(
                    kind="audit", level="INFO", logger="app.admin.users", message="Audit event"
                )
                for _ in range(6)
            ]
        )
    writer = SystemLogWriter(app.state.session_factory, retained_limit=3, flush_interval=0.01)
    writer.start()
    try:
        async with asyncio.timeout(2):
            while True:
                async with app.state.session_factory() as session:
                    count = await session.scalar(select(func.count(SystemLog.id)))
                if count == 3:
                    break
                await asyncio.sleep(0.01)
        assert writer.persisted_count == 0
    finally:
        await writer.stop()


async def test_database_status_returns_counts_version_latest_sample_and_pool(
    client, app, make_account
):
    admin = await make_account("system-database@example.com", "admin")
    user = await make_account("system-database-user@example.com")
    created = await client.post(
        "/api/v1/records",
        headers=user.headers,
        json={"title": "counted", "value": "1.25", "category": "device-001"},
    )
    assert created.status_code == 201
    timestamp = datetime(2026, 11, 8, 1, tzinfo=UTC)
    async with app.state.session_factory.begin() as session:
        session.add(
            RealtimeSample(
                event_id=str(uuid4()),
                title="sample",
                value=Decimal("90"),
                category="cpu",
                timestamp=timestamp,
                threshold=Decimal("80"),
                is_anomaly=True,
            )
        )
    response = await client.get("/api/v1/admin/database", headers=admin.headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "healthy" and data["error"] is None
    assert data["counts"]["users"] == data["counts"]["active_users"] == 2
    assert data["counts"]["records"] == data["counts"]["realtime_samples"] == 1
    assert data["latest_sample_at"] == "2026-11-08T01:00:00Z"
    assert "11.7" in data["server_version"]
    assert data["pool"]["size"] == 5 and data["pool"]["overflow"] >= 0
    assert data["latency_ms"] >= 0 and "password" not in response.text
    status = await client.get("/api/v1/admin/logging-status", headers=admin.headers)
    assert status.status_code == 200
    assert status.json()["queue_capacity"] == 1000 and status.json()["retained_limit"] == 10000
    assert status.json()["writer_running"] is True
