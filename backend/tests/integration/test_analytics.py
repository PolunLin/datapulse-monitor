"""Analytics executes real MariaDB aggregates, including permissions and consistency."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.record import Record
from app.schemas.analytics import AnalyticsFilters
from app.services.monitoring import create_sample_event

pytestmark = pytest.mark.integration


async def insert_records(app, owner_id, *rows):
    async with app.state.session_factory.begin() as session:
        session.add_all(
            [
                Record(
                    title=row.get("title", "sample"),
                    value=Decimal(row.get("value", "1")),
                    category=row.get("category", "cpu"),
                    timestamp=row.get("timestamp", datetime(2026, 11, 8, tzinfo=UTC)),
                    created_by=row.get("created_by", owner_id),
                )
                for row in rows
            ]
        )


async def test_analytics_requires_auth_for_both_sources_and_accepts_all_reader_roles(
    client, make_account
):
    assert (await client.get("/api/v1/analytics")).status_code == 401
    assert (await client.get("/api/v1/analytics", params={"source": "realtime"})).status_code == 401
    for role in ("user", "viewer", "admin"):
        account = await make_account(f"analytics-{role}@example.com", role=role)
        records = await client.get("/api/v1/analytics", headers=account.headers)
        assert records.status_code == 200
        realtime = await client.get(
            "/api/v1/analytics", params={"source": "realtime"}, headers=account.headers
        )
        assert realtime.status_code == 200


async def test_empty_statistics_distinguish_zero_total_from_missing_extrema(client, make_account):
    account = await make_account("analytics-empty@example.com")
    response = await client.get("/api/v1/analytics", headers=account.headers)
    assert response.status_code == 200
    assert response.json() == {
        "source": "records",
        "interval": "day",
        "summary": {"count": 0, "total": "0", "average": None, "minimum": None, "maximum": None},
        "categories": [],
        "trend": [],
    }


async def test_totals_and_category_means_use_all_matching_rows(client, app, make_account):
    owner = await make_account("analytics-owner@example.com")
    viewer = await make_account("analytics-reader@example.com", role="viewer")
    await insert_records(
        app,
        owner.id,
        {"category": "cpu", "value": "1"},
        {"category": "cpu", "value": "3"},
        {"category": "memory", "value": "8"},
    )
    response = await client.get("/api/v1/analytics", headers=viewer.headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["summary"]["count"] == 3
    assert Decimal(data["summary"]["total"]) == 12
    assert Decimal(data["summary"]["average"]) == 4  # Not the mean of category means (5).
    assert Decimal(data["summary"]["minimum"]) == 1
    assert Decimal(data["summary"]["maximum"]) == 8
    assert [row["category"] for row in data["categories"]] == ["cpu", "memory"]
    assert [row["count"] for row in data["categories"]] == [2, 1]
    assert [Decimal(row["average"]) for row in data["categories"]] == [2, 8]
    assert data["trend"][0]["count"] == 3


async def test_decimal_precision_is_preserved_for_sum_larger_than_record_limit(
    client, app, make_account
):
    account = await make_account("analytics-decimal@example.com")
    await insert_records(
        app,
        account.id,
        {"value": "999999999999.999999"},
        {"value": "999999999999.999997"},
    )
    data = (await client.get("/api/v1/analytics", headers=account.headers)).json()
    for statistics in (data["summary"], data["categories"][0], data["trend"][0]):
        assert isinstance(statistics["total"], str)
        assert Decimal(statistics["total"]) == Decimal("1999999999999.999996")
        assert Decimal(statistics["average"]) == Decimal("999999999999.999998")
        assert Decimal(statistics["minimum"]) == Decimal("999999999999.999997")
        assert Decimal(statistics["maximum"]) == Decimal("999999999999.999999")


async def test_filters_use_half_open_utc_ranges_and_escape_title_wildcards(
    client, app, make_account
):
    owner = await make_account("analytics-filter-owner@example.com")
    other = await make_account("analytics-filter-other@example.com")
    start = datetime(2026, 11, 8, 1, tzinfo=UTC)
    await insert_records(
        app,
        owner.id,
        {"title": "literal %_ title", "value": "2", "timestamp": start},
        {"title": "literal xx title", "value": "5", "timestamp": start},
        {"title": "literal %_ title", "timestamp": start, "created_by": other.id},
        {"title": "literal %_ title", "timestamp": start, "category": "memory"},
        {"title": "literal %_ title", "timestamp": start - timedelta(microseconds=1)},
        {"title": "literal %_ title", "timestamp": start + timedelta(hours=1)},
    )
    response = await client.get(
        "/api/v1/analytics",
        params={
            "q": "%_",
            "category": "cpu",
            "created_by": owner.id,
            "start_time": "2026-11-08T09:00:00+08:00",
            "end_time": "2026-11-08T10:00:00+08:00",
            "interval": "hour",
        },
        headers=owner.headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["summary"]["count"] == 1
    assert Decimal(data["summary"]["total"]) == 2
    assert data["trend"][0]["timestamp"] == "2026-11-08T01:00:00Z"


@pytest.mark.parametrize("interval, expected_count", [("hour", 3), ("day", 3)])
async def test_trend_buckets_are_taiwan_ordered_and_do_not_invent_empty_buckets(
    client, app, make_account, interval, expected_count
):
    account = await make_account("analytics-buckets@example.com")
    start = datetime(2026, 11, 8, tzinfo=UTC)
    await insert_records(
        app,
        account.id,
        {"timestamp": start + timedelta(hours=23, minutes=59), "value": "2"},
        {"timestamp": start, "value": "1"},
        {"timestamp": start + timedelta(days=2), "value": "3"},
    )
    data = (
        await client.get(
            "/api/v1/analytics", params={"interval": interval}, headers=account.headers
        )
    ).json()
    trend = data["trend"]
    assert len(trend) == expected_count
    assert [point["timestamp"] for point in trend] == sorted(point["timestamp"] for point in trend)
    assert trend[0]["timestamp"] == (
        "2026-11-08T00:00:00Z" if interval == "hour" else "2026-11-07T16:00:00Z"
    )
    assert trend[-1]["timestamp"] == (
        "2026-11-10T00:00:00Z" if interval == "hour" else "2026-11-09T16:00:00Z"
    )
    assert sum(point["count"] for point in trend) == 3


async def test_daily_statistics_split_at_taiwan_midnight_and_keep_half_open_range(
    client, app, make_account
):
    account = await make_account("analytics-taipei-midnight@example.com")
    boundary = datetime(2026, 11, 8, 16, tzinfo=UTC)
    await insert_records(
        app,
        account.id,
        {"timestamp": boundary - timedelta(microseconds=1), "value": "1"},
        {"timestamp": boundary, "value": "2"},
        {"timestamp": boundary + timedelta(hours=7), "value": "3"},
        {"timestamp": boundary + timedelta(days=1), "value": "99"},
    )
    data = (
        await client.get(
            "/api/v1/analytics",
            params={
                "interval": "day",
                "start_time": "2026-11-08T00:00:00+08:00",
                "end_time": "2026-11-10T00:00:00+08:00",
            },
            headers=account.headers,
        )
    ).json()
    assert data["summary"]["count"] == 3
    assert [(row["timestamp"], row["count"]) for row in data["trend"]] == [
        ("2026-11-07T16:00:00Z", 1),
        ("2026-11-08T16:00:00Z", 2),
    ]
    assert [Decimal(row["total"]) for row in data["trend"]] == [1, 5]


@pytest.mark.parametrize(
    "instant,bucket",
    [
        (datetime(1000, 1, 1, tzinfo=UTC), "0999-12-31T16:00:00Z"),
        (datetime(1945, 7, 1, 16, tzinfo=UTC), "1945-07-01T16:00:00Z"),
        (datetime.max.replace(tzinfo=UTC), "9999-12-31T16:00:00Z"),
    ],
)
async def test_taiwan_daily_buckets_cover_supported_dates_with_fixed_offset(
    client, app, make_account, instant, bucket
):
    account = await make_account("analytics-taipei-boundary@example.com")
    await insert_records(app, account.id, {"timestamp": instant})
    response = await client.get("/api/v1/analytics", headers=account.headers)
    assert response.status_code == 200, response.text
    assert response.json()["trend"][0]["timestamp"] == bucket


async def test_realtime_statistics_only_include_committed_samples_and_keep_sources_separate(
    client, app, make_account
):
    admin = await make_account("analytics-committed@example.com", role="admin")
    await insert_records(app, admin.id, {"value": "999"})
    event = create_sample_event(value=Decimal("85.123456"), category="cpu")
    await app.state.event_dispatcher.publish(event)
    params = {"source": "realtime", "category": "cpu"}
    pending = await client.get("/api/v1/analytics", params=params, headers=admin.headers)
    assert pending.status_code == 200, pending.text
    assert pending.json()["summary"]["count"] == 0
    assert await app.state.batch_writer.stop(timeout=3)
    committed = await client.get("/api/v1/analytics", params=params, headers=admin.headers)
    assert committed.status_code == 200
    assert committed.json()["summary"]["count"] == 1
    assert Decimal(committed.json()["summary"]["total"]) == Decimal("85.123456")
    records = await client.get("/api/v1/analytics", headers=admin.headers)
    assert Decimal(records.json()["summary"]["total"]) == 999


async def test_viewer_keeps_analytics_access_without_admin_management_access(
    client, app, make_account
):
    from app.models.user import User

    admin = await make_account("analytics-demoted@example.com", role="admin")
    async with app.state.session_factory.begin() as session:
        user = await session.get(User, admin.id)
        user.role = "viewer"
    response = await client.get(
        "/api/v1/analytics", params={"source": "realtime"}, headers=admin.headers
    )
    assert response.status_code == 200
    assert (await client.get("/api/v1/analytics", headers=admin.headers)).status_code == 200
    assert (await client.get("/api/v1/monitoring/status", headers=admin.headers)).status_code == 403
    assert (
        await client.get("/api/v1/monitoring/history", headers=admin.headers)
    ).status_code == 403


async def test_analytics_keeps_one_snapshot_when_data_changes_between_queries(
    client, app, make_account, monkeypatch
):
    from app.repositories import analytics

    account = await make_account("analytics-snapshot@example.com")
    await insert_records(app, account.id, {"value": "3"})
    original = analytics.summarize

    async def summarize_then_commit_another_record(session, filters: AnalyticsFilters):
        result = await original(session, filters)
        await insert_records(app, account.id, {"value": "99", "category": "new"})
        return result

    monkeypatch.setattr(analytics, "summarize", summarize_then_commit_another_record)
    response = await client.get("/api/v1/analytics", headers=account.headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["summary"]["count"] == 1
    assert sum(row["count"] for row in data["categories"]) == 1
    assert sum(row["count"] for row in data["trend"]) == 1
    assert Decimal(data["summary"]["total"]) == Decimal(data["trend"][0]["total"]) == 3
    monkeypatch.setattr(analytics, "summarize", original)
    later = await client.get("/api/v1/analytics", headers=account.headers)
    assert later.json()["summary"]["count"] == 2


@pytest.mark.parametrize("group", ["categories", "trend"])
async def test_excessive_groups_are_rejected_instead_of_silently_truncated(
    client, app, make_account, group
):
    account = await make_account("analytics-limit@example.com")
    start = datetime(2026, 11, 8, tzinfo=UTC)
    rows = [
        {"category": f"group-{index:04d}"}
        if group == "categories"
        else {"timestamp": start + timedelta(hours=index)}
        for index in range(1001)
    ]
    await insert_records(app, account.id, *rows)
    response = await client.get(
        "/api/v1/analytics", params={"interval": "hour"}, headers=account.headers
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "analytics_limit_exceeded"
    # A narrower request recovers without a server configuration change.
    params = {"category": "group-0000"} if group == "categories" else {"interval": "day"}
    assert (
        await client.get("/api/v1/analytics", params=params, headers=account.headers)
    ).status_code == 200


@pytest.mark.parametrize(
    "params",
    [
        {"source": "users"},
        {"interval": "minute"},
        {"created_by": 0},
        {"source": "realtime", "created_by": 1},
        {"start_time": "2026-11-08T00:00:00"},
        {"start_time": "0001-01-01T00:00:00+14:00"},
        {"start_time": "2026-11-08T00:00:00Z", "end_time": "2026-11-08T00:00:00Z"},
        {"category": " "},
        {"page_size": 20},
    ],
)
async def test_analytics_rejects_invalid_or_unsupported_filters(client, make_account, params):
    account = await make_account("analytics-invalid@example.com", role="admin")
    response = await client.get("/api/v1/analytics", params=params, headers=account.headers)
    assert response.status_code == 422, response.text
