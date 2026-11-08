from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.dependencies import CurrentUser
from app.core.openapi import error_responses
from app.realtime.devices import configured_devices
from app.schemas.device import LEGACY_DEVICE_ID, LEGACY_DEVICE_NAME, DeviceCatalog, DeviceRead
from app.schemas.monitoring import (
    MonitoringSampleFilters,
    MonitoringStatus,
    SampleFeed,
    SampleFeedFilters,
    SampleFilters,
    SamplePage,
    WebSocketControl,
    WebSocketStatus,
)
from app.services.authorization import require_admin, require_sample_reader
from app.services.monitoring_history import MonitoringHistoryService

router = APIRouter(prefix="/monitoring", tags=["Monitoring"], responses=error_responses(401, 403))


@router.get("/devices", response_model=DeviceCatalog)
async def devices(request: Request, principal: CurrentUser) -> DeviceCatalog:
    """Configured simulated equipment plus an explicit identity for older records."""
    require_sample_reader(principal)
    settings = request.app.state.settings
    items = [
        DeviceRead.model_validate(device)
        for device in configured_devices(settings.simulator_category, settings.simulator_threshold)
    ]
    items.append(
        DeviceRead(
            id=LEGACY_DEVICE_ID,
            name=LEGACY_DEVICE_NAME,
            category=None,
            unit="",
            threshold=None,
        )
    )
    return DeviceCatalog(items=items, sample_interval_seconds=settings.simulator_interval_seconds)


@router.get("/sample-feed", response_model=SampleFeed, responses=error_responses(422))
async def sample_feed(
    request: Request,
    principal: CurrentUser,
    filters: Annotated[SampleFeedFilters, Query()],
) -> SampleFeed:
    """Device-scoped committed samples in insertion order, without counting history.

    Omit after_id for the latest bounded snapshot. Pass next_cursor as after_id
    to fetch later commits; has_more means another bounded page is available.
    A quiet feed preserves its cursor. Cursors are based on the single writer's
    serial inserts, not measurement timestamps, so delayed older events are read.
    """
    return await MonitoringHistoryService(request.app.state.session_factory).feed(
        principal, filters
    )


@router.get("/samples", response_model=SamplePage, responses=error_responses(422))
async def samples(
    request: Request,
    principal: CurrentUser,
    filters: Annotated[MonitoringSampleFilters, Query()],
) -> SamplePage:
    """Authenticated monitoring history, newest first, including persisted past sessions.

    WebSocket events remain pending until their batch commits. Use event_id to reconcile
    historical and live samples, and start_time/end_time for a half-open UTC time range.
    q matches literal title text, using the same semantics as analytics and exports.
    """
    return await MonitoringHistoryService(request.app.state.session_factory).list_monitoring(
        principal, filters
    )


@router.get("/history", response_model=SamplePage, responses=error_responses(422))
async def history(
    request: Request, principal: CurrentUser, filters: Annotated[SampleFilters, Query()]
) -> SamplePage:
    """Admin-only committed samples. Pending WebSocket events appear after a batch commits."""
    return await MonitoringHistoryService(request.app.state.session_factory).list(
        principal, filters
    )


@router.get("/status", response_model=MonitoringStatus)
async def pipeline_status(request: Request, principal: CurrentUser) -> MonitoringStatus:
    """Current-process counters; they reset on restart, unlike persisted history."""
    require_admin(principal)
    state = request.app.state
    writer, simulator = state.batch_writer, state.simulator
    return MonitoringStatus(
        simulator_enabled=state.settings.simulator_enabled,
        generator_running=simulator is not None
        and simulator.task is not None
        and not simulator.task.done(),
        generated_count=simulator.generated_count if simulator is not None else 0,
        writer_running=writer.task is not None and not writer.task.done(),
        writer_healthy=writer.healthy,
        pending_count=writer.pending_count,
        persisted_count=writer.persisted_count,
        failure_count=writer.failure_count,
        last_error=writer.last_error,
        websocket_enabled=state.ws_manager.enabled,
        websocket_connections=state.ws_manager.connection_count,
        batch_size=state.settings.realtime_batch_size,
        flush_interval_seconds=state.settings.realtime_flush_interval_seconds,
    )


@router.get("/websocket", response_model=WebSocketStatus)
async def websocket_status(request: Request, principal: CurrentUser) -> WebSocketStatus:
    """Admin-only runtime state of this process's WebSocket service."""
    require_admin(principal)
    manager = request.app.state.ws_manager
    return WebSocketStatus(enabled=manager.enabled, connections=manager.connection_count)


@router.put("/websocket", response_model=WebSocketStatus, responses=error_responses(422))
async def control_websocket(
    request: Request, principal: CurrentUser, payload: WebSocketControl
) -> WebSocketStatus:
    """Toggle WebSocket delivery without restarting REST, simulation or persistence.

    Disabling also closes existing sockets; enabling permits fresh authenticated
    connections. Runtime changes reset to WS_ENABLED when the process restarts.
    """
    require_admin(principal)
    manager = request.app.state.ws_manager
    await manager.set_enabled(payload.enabled)
    return WebSocketStatus(enabled=manager.enabled, connections=manager.connection_count)
