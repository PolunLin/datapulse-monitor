from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.dependencies import CurrentUser
from app.core.openapi import error_responses
from app.schemas.system import DatabaseStatus, LogFilters, LoggingStatus, LogPage
from app.services.authorization import require_admin
from app.services.database_status import DatabaseStatusService
from app.services.system_logs import SystemLogService

router = APIRouter(prefix="/admin", tags=["Administration"], responses=error_responses(401, 403))


@router.get("/logs", response_model=LogPage, responses=error_responses(422))
async def logs(
    request: Request, principal: CurrentUser, filters: Annotated[LogFilters, Query()]
) -> LogPage:
    """Persisted application logs, newest first. Passwords, tokens and raw payloads are excluded."""
    return await SystemLogService(request.app.state.session_factory).list(principal, filters)


@router.get("/logging-status", response_model=LoggingStatus)
async def logging_status(request: Request, principal: CurrentUser) -> LoggingStatus:
    """Best-effort runtime log queue counters reset on restart; database logs persist."""
    require_admin(principal)
    return request.app.state.system_log_writer.snapshot()


@router.get("/database", response_model=DatabaseStatus)
async def database_status(request: Request, principal: CurrentUser) -> DatabaseStatus:
    """ORM schema/read probe, row counts and this process's SQLAlchemy pool snapshot."""
    state = request.app.state
    return await DatabaseStatusService(state.session_factory, state.engine, state.settings).get(
        principal
    )
