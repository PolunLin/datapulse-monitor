import logging
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from app.db.health import check_database

router = APIRouter(prefix="/health", tags=["Health"])
logger = logging.getLogger("app.health")


class ReadinessUnavailable(BaseModel):
    status: Literal["unavailable", "degraded"]


@router.get("/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/ready",
    responses={
        503: {
            "model": ReadinessUnavailable,
            "description": "Database unavailable or realtime pipeline not ready",
        }
    },
)
async def readiness(request: Request, response: Response) -> dict[str, str]:
    try:
        await check_database(request.app.state.session_factory)
    except SQLAlchemyError:
        logger.warning("Database readiness check failed", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable"}
    state = request.app.state
    writer = state.batch_writer
    simulator = state.simulator
    if (
        not writer.healthy
        or writer.task is None
        or writer.task.done()
        or (simulator is not None and not simulator.healthy)
    ):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded"}
    return {"status": "ok"}
