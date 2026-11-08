from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status

from app.api.dependencies import CurrentUser, RecordServiceDependency
from app.core.openapi import error_responses
from app.schemas.record import RecordCreate, RecordFilters, RecordPage, RecordRead, RecordUpdate

router = APIRouter(prefix="/records", tags=["Records"], responses=error_responses(401))
RecordId = Annotated[int, Path(ge=1)]


@router.post(
    "",
    response_model=RecordRead,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(403, 422),
)
async def create_record(
    payload: RecordCreate, principal: CurrentUser, service: RecordServiceDependency
) -> RecordRead:
    return await service.create(principal, payload)


@router.get("", response_model=RecordPage, responses=error_responses(422))
async def list_records(
    filters: Annotated[RecordFilters, Query()],
    principal: CurrentUser,
    service: RecordServiceDependency,
) -> RecordPage:
    """Authenticated users share read access. Time ranges are [start_time, end_time)."""
    return await service.list(filters)


@router.get("/{record_id}", response_model=RecordRead, responses=error_responses(404, 422))
async def get_record(
    record_id: RecordId, principal: CurrentUser, service: RecordServiceDependency
) -> RecordRead:
    return await service.get(record_id)


@router.patch("/{record_id}", response_model=RecordRead, responses=error_responses(403, 404, 422))
async def update_record(
    record_id: RecordId,
    payload: RecordUpdate,
    principal: CurrentUser,
    service: RecordServiceDependency,
) -> RecordRead:
    return await service.update(principal, record_id, payload)


@router.delete(
    "/{record_id}", status_code=status.HTTP_204_NO_CONTENT, responses=error_responses(403, 404, 422)
)
async def delete_record(
    record_id: RecordId, principal: CurrentUser, service: RecordServiceDependency
) -> Response:
    await service.delete(principal, record_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
