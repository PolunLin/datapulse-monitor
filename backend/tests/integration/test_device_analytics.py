"""Device identity stays attached to filtered analysis and exported history."""

import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from openpyxl import load_workbook

from app.models.realtime import RealtimeSample
from app.models.record import Record

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("role", ["user", "viewer", "admin"])
async def test_device_analysis_and_export_preserve_identity_and_filter(
    client, app, make_account, role
):
    account = await make_account("device-analysis@example.com", role)
    async with app.state.session_factory.begin() as session:
        for device, value in (
            ("device-001", "30.123456"),
            ("device-002", "90.000001"),
            ("legacy", "7"),
        ):
            session.add(
                RealtimeSample(
                    event_id=str(uuid4()),
                    title="Temperature",
                    value=Decimal(value),
                    category="temperature",
                    timestamp=datetime(2026, 11, 8, tzinfo=UTC),
                    threshold=Decimal("80"),
                    is_anomaly=Decimal(value) > 80,
                    device_id=device,
                    device_name=f"設備 {device}",
                    unit="°C",
                )
            )
    filters = {"source": "realtime", "device_id": "device-001"}
    response = await client.get("/api/v1/analytics", headers=account.headers, params=filters)
    assert response.status_code == 200, response.text
    assert response.json()["summary"]["count"] == 1
    assert Decimal(response.json()["summary"]["total"]) == Decimal("30.123456")
    assert response.json()["categories"][0]["category"] == "device-001"
    unfiltered = await client.get(
        "/api/v1/analytics", headers=account.headers, params={"source": "realtime"}
    )
    assert [row["category"] for row in unfiltered.json()["categories"]] == [
        "device-001",
        "device-002",
        "legacy",
    ]
    exported = await client.get("/api/v1/record-exports", headers=account.headers, params=filters)
    assert exported.status_code == 200
    workbook = load_workbook(io.BytesIO(exported.content))
    try:
        rows = list(workbook["Data"].values)
        assert len(rows) == 2
        item = dict(zip(rows[0], rows[1], strict=True))
        assert item["device_id"] == "device-001"
        assert item["device_name"] == "設備 device-001"
        assert item["unit"] == "°C"
        assert item["value"] == "30.123456"
        assert item["timestamp_taipei"] == "2026-11-08T08:00:00+08:00"
        metadata = dict(list(workbook["Metadata"].values)[1:])
        assert metadata["Filter: device_id"] == "device-001"
    finally:
        workbook.close()


@pytest.mark.parametrize("path", ["/api/v1/analytics", "/api/v1/record-exports"])
@pytest.mark.parametrize(
    "filters",
    [
        {"source": "records", "device_id": "device-001"},
        {"source": "realtime", "device_id": "../device"},
        {"source": "realtime", "device_id": " "},
        {"source": "records", "is_anomaly": True},
        {"source": "records", "is_anomaly": False},
        {"source": "records", "event_id": str(uuid4())},
        {"source": "realtime", "event_id": "invalid-uuid"},
        {"source": "realtime", "is_anomaly": "maybe"},
    ],
)
async def test_device_filters_reject_unsupported_source_and_invalid_ids(
    client, make_account, path, filters
):
    admin = await make_account("device-invalid@example.com", "admin")
    response = await client.get(path, headers=admin.headers, params=filters)
    assert response.status_code == 422


@pytest.mark.parametrize("role", ["user", "viewer", "admin"])
async def test_personal_record_filters_separate_owners_and_system_source(
    client, app, make_account, role
):
    owner = await make_account("personal-analysis@example.com", role)
    other = await make_account("other-analysis@example.com")
    timestamp = datetime(2026, 11, 8, 16, tzinfo=UTC)
    async with app.state.session_factory.begin() as session:
        session.add_all(
            [
                Record(
                    title="personal first",
                    value=Decimal("1.25"),
                    category="device-001",
                    created_by=owner.id,
                    timestamp=timestamp,
                ),
                Record(
                    title="personal second",
                    value=Decimal("2.50"),
                    category="device-001",
                    created_by=owner.id,
                    timestamp=timestamp,
                ),
                Record(
                    title="other owner",
                    value=Decimal("999"),
                    category="device-001",
                    created_by=other.id,
                    timestamp=timestamp,
                ),
                Record(
                    title="other device",
                    value=Decimal("100"),
                    category="device-002",
                    created_by=owner.id,
                    timestamp=timestamp,
                ),
                RealtimeSample(
                    title="system generated",
                    value=Decimal("90"),
                    category="temperature",
                    device_id="device-001",
                    device_name="設備 A",
                    unit="°C",
                    event_id=str(uuid4()),
                    threshold=Decimal("80"),
                    is_anomaly=True,
                    timestamp=timestamp,
                ),
            ]
        )
    filters = {"source": "records", "category": "device-001", "created_by": owner.id}
    response = await client.get("/api/v1/analytics", params=filters, headers=owner.headers)
    assert response.status_code == 200, response.text
    statistics = response.json()
    assert statistics["summary"]["count"] == 2
    assert Decimal(statistics["summary"]["total"]) == Decimal("3.75")
    assert [row["category"] for row in statistics["categories"]] == ["device-001"]
    history = await client.get(
        "/api/v1/records",
        params={key: value for key, value in filters.items() if key != "source"},
        headers=owner.headers,
    )
    assert history.status_code == 200
    assert history.json()["total"] == 2
    assert {row["created_by"] for row in history.json()["items"]} == {owner.id}
    response = await client.get("/api/v1/record-exports", params=filters, headers=owner.headers)
    assert response.status_code == 200, response.text
    workbook = load_workbook(io.BytesIO(response.content))
    try:
        data = list(workbook["Data"].values)
        assert len(data) == 3
        rows = [dict(zip(data[0], row, strict=True)) for row in data[1:]]
        assert {row["created_by"] for row in rows} == {owner.id}
        assert {row["title"] for row in rows} == {"personal first", "personal second"}
        metadata = dict(list(workbook["Metadata"].values)[1:])
        assert metadata["Filter: created_by"] == owner.id
    finally:
        workbook.close()
    system = await client.get(
        "/api/v1/analytics",
        params={"source": "realtime", "device_id": "device-001"},
        headers=owner.headers,
    )
    assert system.status_code == 200
    assert system.json()["summary"]["count"] == 1
    assert Decimal(system.json()["summary"]["total"]) == 90


async def test_system_history_summary_and_export_share_device_title_time_and_alert_filters(
    client, app, make_account
):
    account = await make_account("system-parity@example.com", "viewer")
    start = datetime(2026, 11, 8, 16, tzinfo=UTC)
    end = start + timedelta(hours=1)
    selected_ids = [str(uuid4()), str(uuid4())]
    specifications = [
        (selected_ids[0], "literal %_ first", "device-001", "90", start),
        (selected_ids[1], "literal %_ second", "device-001", "85", start + timedelta(seconds=1)),
        (str(uuid4()), "literal xx title", "device-001", "99", start),
        (str(uuid4()), "literal %_ other device", "device-002", "99", start),
        (str(uuid4()), "literal %_ before", "device-001", "99", start - timedelta(microseconds=1)),
        (str(uuid4()), "literal %_ end", "device-001", "99", end),
        (str(uuid4()), "literal %_ normal", "device-001", "70", start),
    ]
    async with app.state.session_factory.begin() as session:
        session.add_all(
            [
                RealtimeSample(
                    event_id=event_id,
                    title=title,
                    device_id=device_id,
                    device_name=device_id,
                    unit="°C",
                    value=Decimal(value),
                    category="temperature",
                    timestamp=timestamp,
                    threshold=Decimal("80"),
                    is_anomaly=Decimal(value) > 80,
                )
                for event_id, title, device_id, value, timestamp in specifications
            ]
        )
    base = {
        "source": "realtime",
        "device_id": "device-001",
        "q": "%_",
        "is_anomaly": True,
        "start_time": "2026-11-09T00:00:00+08:00",
        "end_time": "2026-11-09T01:00:00+08:00",
    }
    for filters, expected_ids, expected_total in (
        (base, set(selected_ids), Decimal("175")),
        ({**base, "event_id": selected_ids[0]}, {selected_ids[0]}, Decimal("90")),
    ):
        response = await client.get("/api/v1/analytics", params=filters, headers=account.headers)
        assert response.status_code == 200, response.text
        stats = response.json()
        assert stats["summary"]["count"] == len(expected_ids)
        assert Decimal(stats["summary"]["total"]) == expected_total
        history = await client.get(
            "/api/v1/monitoring/samples",
            params={key: value for key, value in filters.items() if key != "source"},
            headers=account.headers,
        )
        assert history.status_code == 200, history.text
        assert history.json()["total"] == len(expected_ids)
        assert {row["event_id"] for row in history.json()["items"]} == expected_ids
        response = await client.get(
            "/api/v1/record-exports", params=filters, headers=account.headers
        )
        assert response.status_code == 200, response.text
        workbook = load_workbook(io.BytesIO(response.content))
        try:
            data = list(workbook["Data"].values)
            rows = [dict(zip(data[0], row, strict=True)) for row in data[1:]]
            assert {row["event_id"] for row in rows} == expected_ids
            metadata = dict(list(workbook["Metadata"].values)[1:])
            assert metadata["Filter: q"] == "%_"
            assert metadata["Filter: is_anomaly"] is True
            assert metadata["Filter: event_id"] == filters.get("event_id", "(all)")
        finally:
            workbook.close()
    normal = await client.get(
        "/api/v1/analytics", params={**base, "is_anomaly": False}, headers=account.headers
    )
    assert normal.status_code == 200
    assert normal.json()["summary"]["count"] == 1
    assert Decimal(normal.json()["summary"]["total"]) == 70
