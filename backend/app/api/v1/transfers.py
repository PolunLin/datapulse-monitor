from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from app.api.dependencies import CurrentUser
from app.core.openapi import error_responses
from app.schemas.analytics import AnalyticsFilters
from app.schemas.transfers import ImportErrorResponse, ImportResult
from app.services.authorization import require_record_writer
from app.services.transfers import (
    MAX_UPLOAD_BYTES,
    XLSX_MEDIA_TYPE,
    ImportTooLargeError,
    TransferService,
    file_issue,
)

router = APIRouter(tags=["Data transfers"], responses=error_responses(401, 403))
MAX_MULTIPART_BYTES = MAX_UPLOAD_BYTES + 64 * 1024


async def read_upload(request: Request) -> tuple[str, bytes]:
    """Bound the entire body before multipart parsing can spool a file to disk."""
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise file_issue("format", "Use multipart/form-data with one file field")
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_MULTIPART_BYTES:
            raise ImportTooLargeError()
        chunks.append(chunk)
    body = b"".join(chunks)

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    bounded_request = Request(request.scope, receive)
    try:
        async with bounded_request.form(
            max_files=1, max_fields=0, max_part_size=MAX_UPLOAD_BYTES
        ) as form:
            entries = list(form.multi_items())
            if (
                len(entries) != 1
                or entries[0][0] != "file"
                or not isinstance(entries[0][1], UploadFile)
            ):
                raise file_issue("format", "Upload exactly one file using the file field")
            upload = entries[0][1]
            content = await upload.read(MAX_UPLOAD_BYTES + 1)
            if len(content) > MAX_UPLOAD_BYTES:
                raise ImportTooLargeError()
            return upload.filename or "", content
    except (HTTPException, MultipartParseError) as exc:
        raise file_issue("format", "Multipart upload must contain exactly one file") from exc


@router.post(
    "/record-imports",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(413, 422, model=ImportErrorResponse),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                        "additionalProperties": False,
                    }
                }
            },
        }
    },
)
async def import_records(request: Request, principal: CurrentUser) -> ImportResult:
    """Import up to 5000 CSV/JSON rows (2 MiB). Validation failure writes no rows."""
    require_record_writer(principal)
    filename, content = await read_upload(request)
    return await TransferService(request.app.state.session_factory).import_records(
        principal, filename, content
    )


@router.get(
    "/record-exports",
    response_class=Response,
    responses={
        **error_responses(422),
        200: {"content": {XLSX_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}}},
    },
)
async def export_records(
    request: Request, principal: CurrentUser, filters: Annotated[AnalyticsFilters, Query()]
) -> Response:
    """Download up to 10000 filtered committed rows from either source after login.

    Use created_by for personal records and device_id for a system-generated
    device. Export filters match analytics; source tables are never combined.
    """
    content = await TransferService(request.app.state.session_factory).export_records(
        principal, filters
    )
    return Response(
        content=content,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="datapulse-{filters.source}.xlsx"',
            "Cache-Control": "no-store",
        },
    )
