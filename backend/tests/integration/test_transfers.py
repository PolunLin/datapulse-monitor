import io
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.models.realtime import RealtimeSample
from app.models.record import Record

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_csv_import_assigns_owner_and_preserves_precision(client, make_account):
    account = await make_account("csv-import@example.com")
    content = (
        "\ufefftitle,value,category,timestamp\n"
        "precision,123456789012.123456,device-001,2026-11-08T09:00:00+08:00\n"
        "current,-0.000001,device-001,\n"
    ).encode()
    response = await client.post(
        "/api/v1/record-imports",
        headers=account.headers,
        files={"file": ("sample.csv", content, "text/csv")},
    )
    assert response.status_code == 201, response.text
    assert response.json() == {"imported_count": 2}
    records = (await client.get("/api/v1/records", headers=account.headers)).json()["items"]
    assert len(records) == 2
    assert {record["created_by"] for record in records} == {account.id}
    exact = next(record for record in records if record["title"] == "precision")
    assert exact["value"] == "123456789012.123456"
    assert exact["timestamp"] == "2026-11-08T01:00:00Z"


async def test_json_numeric_import_preserves_decimal(client, make_account):
    account = await make_account("json-import@example.com", "admin")
    response = await client.post(
        "/api/v1/record-imports",
        headers=account.headers,
        files={
            "file": (
                "sample.json",
                b'[{"title":"precise","value":123456789012.123456,"category":"device-001"}]',
            )
        },
    )
    assert response.status_code == 201, response.text
    records = (await client.get("/api/v1/records", headers=account.headers)).json()["items"]
    assert records[0]["value"] == "123456789012.123456"


async def test_invalid_import_is_atomic_and_errors_expose_no_inputs(client, make_account, app):
    account = await make_account("atomic-import@example.com")
    payload = [
        {"title": "valid", "value": 1, "category": "device-001"},
        {"title": "PRIVATE", "value": "NaN", "category": "device-001"},
        {"title": "spoof", "value": 2, "category": "device-001", "created_by": account.id + 1},
    ]
    response = await client.post(
        "/api/v1/record-imports",
        headers=account.headers,
        files={"file": ("sample.json", json.dumps(payload).encode())},
    )
    assert response.status_code == 422
    result = response.json()
    assert result["error"]["code"] == "import_validation"
    assert result["request_id"] == response.headers["x-request-id"]
    assert result["error"]["details"]["total_errors"] == 2
    assert [issue["row"] for issue in result["error"]["details"]["errors"]] == [2, 3]
    assert "PRIVATE" not in response.text
    async with app.state.session_factory() as session:
        assert await session.scalar(select(func.count(Record.id))) == 0


async def test_import_permissions_checked_before_body_parsing(client, make_account):
    viewer = await make_account("viewer-import@example.com", "viewer")
    for headers, expected in (({}, 401), (viewer.headers, 403)):
        response = await client.post(
            "/api/v1/record-imports", headers=headers, content=b"not multipart"
        )
        assert response.status_code == expected


@pytest.mark.parametrize("field_names", [("file", "file"), ("file", "extra"), ("other",)])
async def test_upload_rejects_extra_duplicate_or_wrong_file_fields(
    client, make_account, field_names
):
    account = await make_account("fields-import@example.com")
    response = await client.post(
        "/api/v1/record-imports",
        headers=account.headers,
        files=[(name, ("sample.json", b"[]")) for name in field_names],
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "import_validation"


async def test_upload_rejects_non_file_form_fields_and_malformed_multipart(client, make_account):
    account = await make_account("form-import@example.com")
    response = await client.post(
        "/api/v1/record-imports",
        headers=account.headers,
        files={"file": ("sample.json", b"[]")},
        data={"created_by": "42"},
    )
    assert response.status_code == 422
    response = await client.post(
        "/api/v1/record-imports",
        headers={**account.headers, "content-type": "multipart/form-data"},
        content=b"missing boundary",
    )
    assert response.status_code == 422


async def test_upload_file_and_total_body_have_limits(client, make_account):
    account = await make_account("large-import@example.com")
    # A little above the file limit but below the total multipart limit.
    response = await client.post(
        "/api/v1/record-imports",
        headers=account.headers,
        files={"file": ("sample.csv", b" " * (2 * 1024 * 1024 + 1))},
    )
    assert response.status_code == 413
    # Even without a valid multipart boundary, excess body data is stopped before parsing.
    response = await client.post(
        "/api/v1/record-imports",
        headers={**account.headers, "content-type": "multipart/form-data; boundary=example"},
        content=b" " * (2 * 1024 * 1024 + 64 * 1024 + 1),
    )
    assert response.status_code == 413


async def test_filtered_export_is_precise_literal_taipei_and_stably_ordered(
    client, make_account, record_payload
):
    owner = await make_account("owner-export@example.com")
    viewer = await make_account("viewer-export@example.com", "viewer")
    for title, value, category, timestamp in (
        ("=SUM(1,2)", "123456789012.123456", "device-001", "2026-11-08T01:00:00Z"),
        ("+SUM(2,3)", "-0.000001", "device-001", "2026-11-08T01:00:00Z"),
        ("outside end", "2", "device-001", "2026-11-08T02:00:00Z"),
        ("outside category", "3", "device-002", "2026-11-08T01:00:00Z"),
    ):
        response = await client.post(
            "/api/v1/records",
            headers=owner.headers,
            json={
                **record_payload,
                "title": title,
                "value": value,
                "category": category,
                "timestamp": timestamp,
            },
        )
        assert response.status_code == 201
    response = await client.get(
        "/api/v1/record-exports",
        headers=viewer.headers,
        params={
            "category": "device-001",
            "created_by": owner.id,
            "start_time": "2026-11-08T09:00:00+08:00",
            "end_time": "2026-11-08T10:00:00+08:00",
        },
    )
    assert response.status_code == 200, response.text
    assert (
        response.headers["content-type"]
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert (
        response.headers["content-disposition"] == 'attachment; filename="datapulse-records.xlsx"'
    )
    assert response.headers["cache-control"] == "no-store"
    workbook = load_workbook(io.BytesIO(response.content))
    data = workbook["Data"]
    assert data.max_row == 3
    assert [data.cell(row, 2).value for row in (2, 3)] == ["=SUM(1,2)", "+SUM(2,3)"]
    assert [data.cell(row, 3).value for row in (2, 3)] == ["123456789012.123456", "-0.000001"]
    assert data["B2"].data_type == "s"
    assert data["E1"].value == "timestamp_taipei"
    assert data["E2"].value == "2026-11-08T09:00:00+08:00"
    assert dict(workbook["Metadata"].values)["Exported records"] == 2
    workbook.close()


async def test_realtime_export_accepts_all_readers_and_contains_committed_samples(
    client, make_account, app
):
    admin = await make_account("admin-export@example.com", "admin")
    user = await make_account("user-export@example.com")
    viewer = await make_account("viewer-rt-export@example.com", "viewer")
    event_id = str(uuid4())
    async with app.state.session_factory.begin() as session:
        session.add(
            RealtimeSample(
                event_id=event_id,
                title="committed",
                value=Decimal("90.123456"),
                category="cpu",
                timestamp=datetime(2026, 11, 8, tzinfo=UTC),
                threshold=Decimal("80"),
                is_anomaly=True,
            )
        )
    for headers, status in (({}, 401), (user.headers, 200), (viewer.headers, 200)):
        response = await client.get(
            "/api/v1/record-exports", params={"source": "realtime"}, headers=headers
        )
        assert response.status_code == status
    response = await client.get(
        "/api/v1/record-exports",
        params={"source": "realtime", "category": "cpu"},
        headers=admin.headers,
    )
    assert response.status_code == 200, response.text
    workbook = load_workbook(io.BytesIO(response.content))
    data = workbook["Data"]
    assert data.max_row == 2
    assert data["B2"].value == event_id
    assert data["D2"].value == "90.123456"
    assert data["G2"].value == "80.000000"
    assert data["H2"].value is True
    workbook.close()


async def test_export_rejects_over_limit_without_silently_truncating(client, make_account, app):
    owner = await make_account("cap-export@example.com")
    async with app.state.session_factory.begin() as session:
        session.add_all(
            [
                Record(title="bulk", value=Decimal("1"), category="bulk", created_by=owner.id)
                for _ in range(10001)
            ]
        )
    response = await client.get("/api/v1/record-exports", headers=owner.headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "export_limit"
    # Applying a filter can still produce a usable empty export.
    response = await client.get(
        "/api/v1/record-exports", params={"category": "none"}, headers=owner.headers
    )
    assert response.status_code == 200
    workbook = load_workbook(io.BytesIO(response.content))
    assert workbook["Data"].max_row == 1
    workbook.close()


@pytest.mark.parametrize("extension", ["csv", "json"])
async def test_import_rejects_device_number_and_time_errors_atomically(
    client, app, make_account, extension
):
    import csv

    account = await make_account("device-field-import@example.com")
    good = {
        "title": "設備 A",
        "value": "25.123456",
        "category": "device-001",
        "timestamp": "2026-11-08T12:00:00+08:00",
    }
    rows = [
        good,
        {**good, "category": "device-999"},
        {**good, "value": "not-a-number"},
        {**good, "timestamp": "2026-11-08T12:00:00"},
    ]
    if extension == "json":
        content = json.dumps(rows).encode()
    else:
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=list(good))
        writer.writeheader()
        writer.writerows(rows)
        content = stream.getvalue().encode()
    response = await client.post(
        "/api/v1/record-imports",
        headers=account.headers,
        files={"file": (f"devices.{extension}", content)},
    )
    assert response.status_code == 422, response.text
    details = response.json()["error"]["details"]
    assert details["total_errors"] == 3
    assert {issue["field"] for issue in details["errors"]} == {"category", "value", "timestamp"}
    async with app.state.session_factory() as session:
        assert await session.scalar(select(func.count(Record.id))) == 0
