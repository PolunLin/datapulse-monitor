"""Bounded, atomic record imports and precision-preserving workbook exports."""

import csv
import io
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from typing import Any
from uuid import UUID

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from pydantic import ValidationError
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.core.exceptions import ApplicationError
from app.db.base import utc_now
from app.db.session import SessionFactory
from app.models.record import Record
from app.repositories.analytics import build_conditions, model_for_source
from app.schemas.analytics import AnalyticsFilters
from app.schemas.record import RecordCreate
from app.schemas.transfers import ImportIssue, ImportIssueDetails, ImportResult
from app.services.auth import Principal
from app.services.authorization import require_record_writer, require_sample_reader

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 5000
MAX_IMPORT_ERRORS = 100
MAX_EXPORT_ROWS = 10000
IMPORT_FIELDS = {"title", "value", "category", "timestamp"}
REQUIRED_FIELDS = {"title", "value", "category"}
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XML_INVALID_CHARACTERS = re.compile(
    "[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\ud800-\\udfff\\ufffe\\uffff]"
)


class ImportValidationError(ApplicationError):
    status_code = 422
    code = "import_validation"

    def __init__(self, errors: list[ImportIssue], total_errors: int | None = None) -> None:
        super().__init__("Import validation failed; no records were written")
        total = len(errors) if total_errors is None else total_errors
        self.details = ImportIssueDetails(
            errors=errors[:MAX_IMPORT_ERRORS],
            total_errors=total,
            errors_truncated=total > MAX_IMPORT_ERRORS,
        ).model_dump()


class ImportTooLargeError(ImportValidationError):
    status_code = 413

    def __init__(self) -> None:
        super().__init__(
            [ImportIssue(row=1, field="file", code="limit", message="File exceeds 2 MiB")]
        )


class ExportLimitError(ApplicationError):
    status_code = 422
    code = "export_limit"

    def __init__(self) -> None:
        super().__init__("Export exceeds 10000 records; narrow the filters")


def file_issue(code: str, message: str, *, row: int = 1) -> ImportValidationError:
    return ImportValidationError([ImportIssue(row=row, field="file", code=code, message=message)])


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    raise ValueError("Non-finite JSON value")


def _csv_rows(text: str) -> Iterable[tuple[int, Any]]:
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        headers = next(reader, None)
        if headers is None:
            raise file_issue("empty", "File must contain at least one data row")
        if len(headers) != len(set(headers)):
            raise file_issue("duplicate", "CSV header names must be unique")
        if set(headers) - IMPORT_FIELDS:
            raise file_issue("extra", "CSV contains unsupported columns")
        if not REQUIRED_FIELDS.issubset(headers):
            raise file_issue("missing", "CSV requires title, value and category columns")
        row_number = 2
        for fields in reader:
            if len(fields) != len(headers):
                yield row_number, None
            else:
                item = dict(zip(headers, fields, strict=True))
                if item.get("timestamp") == "":
                    item.pop("timestamp")
                yield row_number, item
            # Logical CSV records, including quoted multiline fields, count as one row.
            row_number += 1
    except csv.Error as exc:
        raise file_issue("format", "CSV is malformed") from exc


def parse_import(filename: str, content: bytes) -> list[RecordCreate]:
    """All CPU parsing/validation executes in a worker thread before any transaction."""
    if len(content) > MAX_UPLOAD_BYTES:
        raise ImportTooLargeError()
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in {"csv", "json"}:
        raise file_issue("format", "Choose a CSV or JSON file")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise file_issue("format", "File must use UTF-8 encoding") from exc

    if extension == "csv":
        rows = _csv_rows(text)
    else:
        try:
            data = json.loads(
                text,
                parse_float=Decimal,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_json_object,
            )
        except (ValueError, RecursionError, DecimalException) as exc:
            raise file_issue("format", "JSON must be valid with unique object keys") from exc
        if not isinstance(data, list):
            raise file_issue("format", "JSON must be an array of record objects")
        rows = enumerate(data, start=1)

    records: list[RecordCreate] = []
    issues: list[ImportIssue] = []
    total_errors = 0
    row_count = 0
    for row_number, item in rows:
        row_count += 1
        if row_count > MAX_IMPORT_ROWS:
            raise file_issue("limit", "Import exceeds 5000 data rows", row=row_number)
        try:
            records.append(RecordCreate.model_validate(item))
        except ValidationError as exc:
            for error in exc.errors(include_input=False, include_context=False, include_url=False):
                total_errors += 1
                if len(issues) >= MAX_IMPORT_ERRORS:
                    continue
                location = error["loc"][0] if error["loc"] else "row"
                field = location if location in IMPORT_FIELDS else "row"
                error_type = error["type"]
                if error_type == "missing":
                    code, message = "missing", "Required field is missing"
                elif error_type == "extra_forbidden":
                    code, message = "extra", "Unsupported field is not allowed"
                else:
                    code = "invalid"
                    message = {
                        "title": "Title must contain 1 to 200 characters",
                        "value": "Use a finite value with at most 12 integer and 6 decimal digits",
                        "category": "Category must be device-001, device-002 or device-003",
                        "timestamp": "Use a zoned timestamp within the supported UTC range",
                        "row": "Each row must be a record object with the expected columns",
                    }[field]
                issues.append(ImportIssue(row=row_number, field=field, code=code, message=message))
    if not row_count:
        raise file_issue("empty", "File must contain at least one data row")
    if total_errors:
        raise ImportValidationError(issues, total_errors)
    return records


def _excel_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        try:
            return value.astimezone(timezone(timedelta(hours=8))).isoformat()
        except OverflowError:
            # Preserve valid API data beyond the local display range without failing
            # the entire download or silently changing the represented instant.
            return f"Outside Taiwan display range; UTC: {value.astimezone(UTC).isoformat()}"
    if isinstance(value, str):
        # XML 1.0 cannot encode these characters: show a reversible, visible escape.
        return XML_INVALID_CHARACTERS.sub(lambda match: f"\\u{ord(match[0]):04x}", value)
    return value


def _append_row(sheet, values: Iterable[Any]) -> None:
    cells = []
    for value in values:
        cell = WriteOnlyCell(sheet, value=_excel_value(value))
        if isinstance(cell.value, str):
            # Literal text, including leading =, +, -, @ and Excel error strings.
            cell.data_type = "s"
            cell.number_format = "@"
        cell.alignment = Alignment(vertical="top", wrap_text=True)
        cells.append(cell)
    # Avoid max_row / row slicing here: those rescan every existing cell on each append.
    sheet.append(cells)


def _style_sheet(sheet, widths: list[int]) -> None:
    for cell in sheet[1]:
        cell.fill = PatternFill("solid", fgColor="00897B")
        cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    sheet.row_dimensions[1].height = 24
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(column)].width = width


def build_workbook(
    rows: list[dict[str, Any]], filters: AnalyticsFilters, exported_at: datetime
) -> bytes:
    workbook = Workbook()
    data = workbook.active
    data.title = "Data"
    if filters.source == "records":
        fields = [
            "id",
            "title",
            "value",
            "category",
            "timestamp",
            "created_by",
            "created_at",
            "updated_at",
        ]
        widths = [12, 38, 25, 26, 32, 14, 32, 32]
    else:
        fields = [
            "id",
            "event_id",
            "title",
            "value",
            "category",
            "timestamp",
            "threshold",
            "is_anomaly",
            "created_at",
            "device_id",
            "device_name",
            "unit",
        ]
        widths = [12, 39, 38, 25, 26, 32, 25, 14, 32, 20, 28, 12]
    headers = [
        f"{name}_taipei" if name in {"timestamp", "created_at", "updated_at"} else name
        for name in fields
    ]
    _append_row(data, headers)
    for row in rows:
        _append_row(data, [row[field] for field in fields])
    _style_sheet(data, widths)

    metadata = workbook.create_sheet("Metadata")
    _append_row(metadata, ["Property", "Value"])
    metadata_rows = [
        ("Source", filters.source),
        ("Exported at (Taiwan UTC+08:00)", exported_at),
        ("Exported records", len(rows)),
        ("Row order", "timestamp ascending, then id ascending"),
        ("Time range", "Inclusive start_time, exclusive end_time; Taiwan ISO 8601 text"),
        ("Display timezone", "Taiwan standard time, fixed UTC+08:00 for all dates"),
        ("Decimal precision", "value and threshold are text to preserve all 18 decimal digits"),
        ("Content", "Committed database records only; export is queried when requested"),
        ("Text handling", "Literal text; XML control characters are shown as \\uXXXX escapes"),
    ]
    for name in (
        "category",
        "q",
        "created_by",
        "device_id",
        "event_id",
        "is_anomaly",
        "start_time",
        "end_time",
    ):
        value = getattr(filters, name)
        metadata_rows.append((f"Filter: {name}", value if value is not None else "(all)"))
    for row in metadata_rows:
        _append_row(metadata, row)
    _style_sheet(metadata, [25, 90])
    for row in range(2, metadata.max_row + 1):
        metadata.row_dimensions[row].height = 32
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


class TransferService:
    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    async def import_records(
        self, principal: Principal, filename: str, content: bytes
    ) -> ImportResult:
        require_record_writer(principal)
        payloads = await run_in_threadpool(parse_import, filename, content)
        async with self.session_factory.begin() as session:
            session.add_all(
                [Record(**payload.model_dump(), created_by=principal.id) for payload in payloads]
            )
        return ImportResult(imported_count=len(payloads))

    async def export_records(self, principal: Principal, filters: AnalyticsFilters) -> bytes:
        if filters.source == "realtime":
            require_sample_reader(principal)
        model = model_for_source(filters.source)
        statement = (
            select(model)
            .where(*build_conditions(filters))
            .order_by(model.timestamp.asc(), model.id.asc())
            .limit(MAX_EXPORT_ROWS + 1)
        )
        async with self.session_factory() as session:
            records = list(await session.scalars(statement))
            if len(records) > MAX_EXPORT_ROWS:
                raise ExportLimitError()
            fields = [column.key for column in model.__table__.columns]
            rows = [{field: getattr(record, field) for field in fields} for record in records]
        return await run_in_threadpool(build_workbook, rows, filters, utc_now())
