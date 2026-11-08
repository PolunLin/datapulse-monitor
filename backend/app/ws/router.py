import asyncio
import logging

from fastapi import APIRouter, WebSocket
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.schemas.device import DeviceId
from app.ws.dependencies import authenticate_websocket

router = APIRouter()
logger = logging.getLogger("app.websocket")
device_id_adapter = TypeAdapter(DeviceId)


@router.websocket("/ws/v1/samples")
async def sample_stream(websocket: WebSocket) -> None:
    manager = websocket.app.state.ws_manager
    if not manager.enabled:
        await manager.reject_unavailable(websocket)
        return
    device_id = websocket.query_params.get("device_id")
    if device_id is not None:
        try:
            device_id = device_id_adapter.validate_python(device_id.strip())
        except ValidationError:
            await websocket.close(code=1008, reason="Invalid device filter")
            return
    category = websocket.query_params.get("category")
    if category is not None:
        category = category.strip()
        if not category or len(category) > 100:
            await websocket.close(code=1008, reason="Invalid category filter")
            return
    try:
        token, principal = await asyncio.wait_for(authenticate_websocket(websocket), timeout=5)
    except (UnauthorizedError, ForbiddenError):
        await websocket.close(code=1008, reason="Invalid credentials")
        return
    except (SQLAlchemyError, TimeoutError):
        logger.warning("WebSocket authentication unavailable", exc_info=True)
        await websocket.close(code=1011, reason="Authentication temporarily unavailable")
        return
    await manager.serve(websocket, token, principal, category=category, device_id=device_id)
