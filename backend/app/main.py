import logging
import re
import time
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from app.api.router import router
from app.api.v1.health import router as health_router
from app.core.config import Settings, get_settings
from app.core.exceptions import ApplicationError
from app.core.logging import configure_logging
from app.lifespan import lifespan
from app.ws.router import router as websocket_router

logger = logging.getLogger("app.requests")


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    details: list | dict | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", str(uuid4()))
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "request_id": request_id},
        headers={"X-Request-ID": request_id, **(headers or {})},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="DataPulse API",
        version="0.5.0",
        description=(
            "JWT authentication, role-protected CRUD and realtime simulator monitoring. "
            "All authenticated users can read records. Users modify their own records; "
            "administrators can modify all records; viewers are read-only. "
            "Logout revokes every existing token for the current account."
            " Authenticated clients can browse equipment and committed samples under /monitoring "
            "and subscribe to /ws/v1/samples with a Bearer header and optional device filter. "
            "The legacy monitoring history endpoint and pipeline status remain Admin-only. "
            "All authenticated roles can analyze and export committed records or equipment "
            "history. Personal analysis filters records by the current user's created_by ID; "
            "record read access remains shared within this workspace. "
            "CSV/JSON record imports are validated and committed atomically. "
            "Admin endpoints manage user roles and account status, inspect persisted application "
            "logs and audit records, and monitor database health. "
            "Errors under /api/v1 return {error: {code, message, details?}, request_id}; "
            "X-Request-ID correlates the response with application logs."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(router)
    app.include_router(health_router)
    app.include_router(websocket_router)

    @app.middleware("http")
    async def request_logging(request: Request, call_next):
        candidate = request.headers.get("X-Request-ID", "")
        request_id = candidate if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", candidate) else str(uuid4())
        request.state.request_id = request_id
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        # Newer FastAPI keeps the leaf APIRoute in scope["route"]. The inclusion
        # context carries its full template, including nested router prefixes.
        route = request.scope.get("fastapi", {}).get(
            "effective_route_context"
        ) or request.scope.get("route")
        level = (
            logging.ERROR
            if response.status_code >= 500
            else logging.WARNING
            if response.status_code >= 400
            else logging.INFO
        )
        logger.log(
            level,
            "HTTP request completed",
            extra={
                "request_id": request_id,
                "method": request.method
                if request.method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                else "OTHER",
                # Log route templates, never raw URLs, path inputs, or query strings.
                "path": getattr(route, "path_format", None)
                or getattr(route, "path", "[unmatched]"),
                "status_code": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "actor_id": getattr(request.state, "actor_id", None),
            },
        )
        return response

    @app.exception_handler(ApplicationError)
    async def application_error_handler(request: Request, exc: ApplicationError):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return error_response(
            request,
            exc.status_code,
            exc.code,
            exc.message,
            details=getattr(exc, "details", None),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        # Pydantic's default input/context can contain passwords or entire request bodies.
        details = [
            {"loc": list(error["loc"]), "type": error["type"], "message": error["msg"]}
            for error in exc.errors()
        ]
        return error_response(
            request, 422, "validation_error", "Request validation failed", details=details
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        return error_response(
            request,
            exc.status_code,
            "http_error",
            str(exc.detail),
            headers=exc.headers,
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error_handler(request: Request, exc: SQLAlchemyError):
        logger.error(
            "Database operation failed",
            exc_info=exc,
            extra={"request_id": request.state.request_id},
        )
        return error_response(
            request, 503, "database_unavailable", "Database operation unavailable"
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        logger.error(
            "Unhandled application error",
            exc_info=exc,
            extra={"request_id": getattr(request.state, "request_id", "unknown")},
        )
        return error_response(request, 500, "internal_error", "An unexpected error occurred")

    return app
