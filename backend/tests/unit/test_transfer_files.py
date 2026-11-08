import io
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from openpyxl import load_workbook
from starlette.requests import Request

from app.api.v1.transfers import MAX_MULTIPART_BYTES, read_upload
from app.schemas.analytics import AnalyticsFilters
from app.services.transfers import (
    ImportTooLargeError,
    ImportValidationError,
    build_workbook,
    parse_import,
)


def test_csv_bom_quoted_fields_blank_timestamp_and_exact_decimal():
    data = (
        "\ufefftitle,value,category,timestamp\r\n"
        '"sensor, north",123456789012.123456,device-001,\r\n'
        '"two\nlines",-0.000001,device-002,2026-11-08T09:30:00+08:00\r\n'
    ).encode()
    result = parse_import("measurements.CSV", data)
    assert result[0].title == "sensor, north"
    assert result[0].value == Decimal("123456789012.123456")
    assert result[0].timestamp.tzinfo is UTC
    assert result[1].title == "two\nlines"
    assert result[1].timestamp == datetime(2026, 11, 8, 1, 30, tzinfo=UTC)


def test_json_numeric_decimal_never_round_trips_through_float():
    rows = parse_import(
        "sample.json",
        b'[{"title":"exact","value":123456789012.123456,"category":"device-001"}]',
    )
    assert rows[0].value == Decimal("123456789012.123456")


@pytest.mark.parametrize(
    ("filename", "content", "code"),
    [
        ("data.txt", b"[]", "format"),
        ("data.csv", b"\xff", "format"),
        ("data.csv", b"", "empty"),
        ("data.csv", b"title,value,category\n", "empty"),
        ("data.csv", b"title,value,value,category\na,1,2,device-001", "duplicate"),
        ("data.csv", b"title,value\na,1", "missing"),
        ("data.csv", b"title,value,category,created_by\na,1,device-001,1", "extra"),
        ("data.csv", b'title,value,category\n"unterminated,1,device-001', "format"),
        ("data.json", b"[]", "empty"),
        ("data.json", b"{}", "format"),
        ("data.json", b"[", "format"),
        ("data.json", b'[{"title":"a","value":NaN,"category":"device-001"}]', "format"),
        (
            "data.json",
            b'[{"title":"a","value":1e99999999999999999999999,"category":"device-001"}]',
            "format",
        ),
        ("data.json", b'[{"title":"a","title":"b","value":1,"category":"device-001"}]', "format"),
    ],
)
def test_invalid_files_fail_with_sanitized_issues(filename, content, code):
    with pytest.raises(ImportValidationError) as caught:
        parse_import(filename, content)
    issue = caught.value.details["errors"][0]
    assert issue["row"] == 1
    assert issue["field"] == "file"
    assert issue["code"] == code


def test_row_errors_report_csv_logical_row_and_no_input_values():
    content = (
        b"title,value,category,timestamp\n"
        b"valid,1,device-001,2026-11-08T00:00:00Z\n"
        b"BAD_SECRET,NaN,device-001,2026-11-08T00:00:00\n"
        b"wrong,2,device-001,2026-11-08T00:00:00Z,extra\n"
    )
    with pytest.raises(ImportValidationError) as caught:
        parse_import("sample.csv", content)
    details = caught.value.details
    assert details["total_errors"] == 3
    assert [(item["row"], item["field"]) for item in details["errors"]] == [
        (3, "value"),
        (3, "timestamp"),
        (4, "row"),
    ]
    assert "BAD_SECRET" not in json.dumps(details)
    assert "NaN" not in json.dumps(details)


def test_json_extra_fields_cannot_inject_owner_or_reflect_arbitrary_keys():
    with pytest.raises(ImportValidationError) as caught:
        parse_import(
            "data.json",
            b'[{"title":"a","value":1,"category":"device-001","created_by":42,"SECRET":true}]',
        )
    details = caught.value.details
    assert details["total_errors"] == 2
    assert {issue["field"] for issue in details["errors"]} == {"row"}
    assert {issue["code"] for issue in details["errors"]} == {"extra"}
    assert "SECRET" not in json.dumps(details)


def test_error_list_is_bounded_and_retains_total_count():
    content = json.dumps([{"title": "a", "value": "NaN", "category": "device-001"}] * 125).encode()
    with pytest.raises(ImportValidationError) as caught:
        parse_import("data.json", content)
    details = caught.value.details
    assert len(details["errors"]) == 100
    assert details["total_errors"] == 125
    assert details["errors_truncated"] is True
    assert details["errors"][-1]["row"] == 100


def test_file_and_row_limits_reject_over_limit_before_any_database_operation():
    with pytest.raises(ImportTooLargeError):
        parse_import("data.json", b" " * (2 * 1024 * 1024 + 1))
    content = b"title,value,category\n" + b"valid,1,device-001\n" * 5001
    with pytest.raises(ImportValidationError) as caught:
        parse_import("data.csv", content)
    assert caught.value.details["errors"][0]["code"] == "limit"
    assert caught.value.details["errors"][0]["row"] == 5002
    records = parse_import("data.csv", b"title,value,category\n" + b"a,1,device-001\n" * 5000)
    assert len(records) == 5000


def test_workbook_literal_strings_decimal_taipei_and_filter_metadata():
    timestamp = datetime(2026, 11, 8, 1, 2, 3, 123456, tzinfo=UTC)
    rows = [
        {
            "id": index,
            "title": title,
            "value": Decimal("123456789012.123456"),
            "category": "line\x00control\ufffe\uffff",
            "timestamp": timestamp,
            "created_by": 7,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        for index, title in enumerate(["=SUM(1,2)", "+cmd", "-cmd", "@cmd", "#REF!"], start=1)
    ]
    filters = AnalyticsFilters(q="=FILTER", start_time=timestamp)
    content = build_workbook(rows, filters, timestamp)
    workbook = load_workbook(io.BytesIO(content), data_only=False)
    assert workbook.sheetnames == ["Data", "Metadata"]
    data = workbook["Data"]
    assert data["C2"].value == "123456789012.123456"
    assert data["C2"].data_type == "s"
    assert data["E1"].value == "timestamp_taipei"
    assert data["E2"].value == "2026-11-08T09:02:03.123456+08:00"
    assert data["D2"].value == "line\\u0000control\\ufffe\\uffff"
    assert [data.cell(row, 2).value for row in range(2, 7)] == [row["title"] for row in rows]
    assert all(data.cell(row, 2).data_type == "s" for row in range(2, 7))
    assert all(cell.data_type != "f" for sheet in workbook for row in sheet for cell in row)
    assert data.freeze_panes == "A2"
    assert data.auto_filter.ref == "A1:H6"
    metadata = dict(workbook["Metadata"].values)
    assert metadata["Filter: q"] == "=FILTER"
    assert metadata["Filter: start_time"] == "2026-11-08T09:02:03.123456+08:00"
    assert metadata["Display timezone"] == "Taiwan standard time, fixed UTC+08:00 for all dates"
    assert metadata["Exported at (Taiwan UTC+08:00)"] == "2026-11-08T09:02:03.123456+08:00"
    workbook.close()


def test_empty_export_still_has_columns_and_zero_record_metadata():
    workbook = load_workbook(
        io.BytesIO(build_workbook([], AnalyticsFilters(source="realtime"), datetime.now(UTC)))
    )
    assert workbook["Data"].max_row == 1
    assert workbook["Data"]["B1"].value == "event_id"
    assert dict(workbook["Metadata"].values)["Exported records"] == 0
    workbook.close()


@pytest.mark.parametrize(
    "timestamp, expected",
    [
        (datetime(2026, 11, 8, 16, tzinfo=UTC), "2026-11-09T00:00:00+08:00"),
        (
            datetime.max.replace(tzinfo=UTC),
            "Outside Taiwan display range; UTC: 9999-12-31T23:59:59.999999+00:00",
        ),
    ],
)
def test_excel_midnight_rollover_and_out_of_display_range_keep_original_instant(
    timestamp, expected
):
    row = {
        "id": 1,
        "title": "device",
        "value": Decimal("1.000001"),
        "category": "device-001",
        "timestamp": timestamp,
        "created_by": 1,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    content = build_workbook([row], AnalyticsFilters(start_time=timestamp), timestamp)
    workbook = load_workbook(io.BytesIO(content))
    assert workbook["Data"]["E2"].value == expected
    assert workbook["Data"]["G2"].value == expected
    assert workbook["Data"]["H2"].value == expected
    assert dict(workbook["Metadata"].values)["Filter: start_time"] == expected
    assert dict(workbook["Metadata"].values)["Exported at (Taiwan UTC+08:00)"] == expected
    workbook.close()


async def test_malformed_multipart_parser_exception_is_safe_validation_error():
    body = b"--example\r\ninvalid header\r\n\r\nPRIVATE\r\n--example--\r\n"

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "app": object(),
            "headers": [(b"content-type", b"multipart/form-data; boundary=example")],
        },
        receive,
    )
    with pytest.raises(ImportValidationError) as caught:
        await read_upload(request)
    assert caught.value.details["errors"][0]["code"] == "format"
    assert "PRIVATE" not in json.dumps(caught.value.details)


async def test_chunked_upload_stops_reading_when_body_exceeds_limit():
    chunks_read = 0

    async def receive():
        nonlocal chunks_read
        chunks_read += 1
        assert chunks_read == 1, "Reader must stop instead of consuming the remaining body"
        return {"type": "http.request", "body": b" " * (MAX_MULTIPART_BYTES + 1), "more_body": True}

    request = Request(
        {
            "type": "http",
            "headers": [(b"content-type", b"multipart/form-data; boundary=example")],
        },
        receive,
    )
    with pytest.raises(ImportTooLargeError):
        await read_upload(request)
    assert chunks_read == 1


@pytest.mark.parametrize("extension", ["csv", "json"])
@pytest.mark.parametrize(
    "field, invalid",
    [
        ("title", ""),
        ("category", "temperature"),
        ("category", "device-004"),
        ("value", "twenty five"),
        ("value", "25°C"),
        ("value", "0.0000001"),
        ("timestamp", "2026-11-08T12:00:00"),
        ("timestamp", "2026-02-30T12:00:00+08:00"),
        ("timestamp", "1794096000"),
    ],
)
def test_csv_and_json_use_the_same_record_field_validation(extension, field, invalid):
    import csv

    record = {
        "title": "設備 A",
        "value": "25.123456",
        "category": "device-001",
        "timestamp": "2026-11-08T12:00:00+08:00",
    }
    rows = [record, {**record, field: invalid}]
    if extension == "json":
        content = json.dumps(rows).encode()
    else:
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=list(record))
        writer.writeheader()
        writer.writerows(rows)
        content = stream.getvalue().encode()
    with pytest.raises(ImportValidationError) as caught:
        parse_import(f"devices.{extension}", content)
    issues = caught.value.details["errors"]
    assert len(issues) == 1
    assert issues[0]["field"] == field
    assert issues[0]["row"] == (3 if extension == "csv" else 2)
