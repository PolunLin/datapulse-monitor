"""Bounded best-effort application logging; never blocks requests on database writes."""

import asyncio
import logging
import math
import re
import threading
from collections import deque
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import and_, delete, or_, select

from app.db.session import SessionFactory
from app.models.system_log import SystemLog
from app.schemas.system import LoggingStatus

QUEUE_CAPACITY = 1000
RETAINED_LIMIT = 10000
METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE", "HEAD", "OPTIONS"}
ROUTE_PATHS = {
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    "/api/v1/users/me",
    "/api/v1/records",
    "/api/v1/records/{record_id}",
    "/api/v1/monitoring/history",
    "/api/v1/monitoring/devices",
    "/api/v1/monitoring/samples",
    "/api/v1/monitoring/status",
    "/api/v1/monitoring/websocket",
    "/api/v1/analytics",
    "/api/v1/record-imports",
    "/api/v1/record-exports",
    "/api/v1/admin/users",
    "/api/v1/admin/users/{user_id}",
    "/api/v1/admin/logs",
    "/api/v1/admin/logging-status",
    "/api/v1/admin/database",
    "/health/live",
    "/health/ready",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/docs/oauth2-redirect",
}
LOGGER_NAMES = {
    "app.requests",
    "app.lifecycle",
    "app.realtime.writer",
    "app.realtime.simulator",
    "app.health",
    "app.websocket",
}
# Use templates as lookup keys without formatting their arguments. Future or unknown
# log messages get a fixed label until explicitly reviewed for sensitive input.
SAFE_MESSAGES = {
    "HTTP request completed": "HTTP request completed",
    "Database operation failed": "Database operation failed",
    "Unhandled application error": "Unhandled application error",
    "Application resources initialized": "Application resources initialized",
    "Application resources released": "Application resources released",
    "Background task failed: %s": "Background task failed",
    "Simulator shutdown failed": "Simulator shutdown failed",
    "Shutdown left %s unconfirmed events in memory": "Shutdown left unconfirmed events in memory",
    "Batch writer shutdown failed": "Batch writer shutdown failed",
    "WebSocket shutdown failed": "WebSocket shutdown failed",
    "Database resource cleanup failed": "Database resource cleanup failed",
    "Batch writer cancellation cleanup exceeded its deadline": "Batch writer cleanup timed out",
    "Batch shutdown timed out with %d pending events": "Batch shutdown timed out",
    "Batch persistence retry after %s": "Batch persistence retry",
    "Batch writer stopped after %s": "Batch writer stopped",
    "Database readiness check failed": "Database readiness check failed",
    "WebSocket authentication unavailable": "WebSocket authentication unavailable",
    "WebSocket transport failed": "WebSocket transport failed",
    "Some WebSocket handlers exceeded the shutdown deadline": "WebSocket shutdown timed out",
    "Simulator stopped after %s": "Simulator stopped",
}


def _safe_identifier(value, limit: int = 64) -> str | None:
    return (
        value
        if isinstance(value, str) and re.fullmatch(rf"[A-Za-z0-9_-]{{1,{limit}}}", value)
        else None
    )


def event_from_record(record: logging.LogRecord) -> dict:
    level = next(
        (
            label
            for threshold, label in ((50, "CRITICAL"), (40, "ERROR"), (30, "WARNING"), (20, "INFO"))
            if record.levelno >= threshold
        ),
        "DEBUG",
    )
    request = record.name == "app.requests" and record.msg == "HTTP request completed"
    duration = getattr(record, "duration_ms", None)
    if type(duration) not in {int, float} or not math.isfinite(duration) or duration < 0:
        duration = None
    status = getattr(record, "status_code", None)
    status = status if type(status) is int and 100 <= status <= 599 else None
    actor_id = getattr(record, "actor_id", None)
    actor_id = actor_id if type(actor_id) is int and 0 < actor_id <= 2147483647 else None
    error_type = (
        type(record.exc_info[1]).__name__ if record.exc_info and record.exc_info[1] else None
    )
    message = (
        SAFE_MESSAGES.get(record.msg, "Application event")
        if isinstance(record.msg, str)
        else "Application event"
    )
    path = getattr(record, "path", None)
    method = getattr(record, "method", None)
    return {
        "event_id": str(uuid4()),
        "timestamp": datetime.fromtimestamp(record.created, UTC),
        "level": level,
        "kind": "request" if request else "runtime",
        "logger": record.name if record.name in LOGGER_NAMES else "app.other",
        "message": message,
        "request_id": _safe_identifier(getattr(record, "request_id", None)),
        "method": method if request and method in METHODS else ("OTHER" if request else None),
        "path": (path if path in ROUTE_PATHS else "[unmatched]") if request else None,
        "status_code": status if request else None,
        "duration_ms": duration if request else None,
        "error_type": _safe_identifier(error_type, 80),
        "actor_id": actor_id,
    }


class DatabaseLogHandler(logging.Handler):
    def __init__(self, writer: "SystemLogWriter") -> None:
        super().__init__()
        self.writer = writer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.writer.enqueue(event_from_record(record))
        except Exception:
            # Logging must not break requests or recurse into another failing handler.
            self.writer.record_drop()


class SystemLogWriter:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        queue_capacity: int = QUEUE_CAPACITY,
        retained_limit: int = RETAINED_LIMIT,
        batch_size: int = 100,
        flush_interval: float = 1.0,
    ) -> None:
        if min(queue_capacity, retained_limit, batch_size) <= 0 or flush_interval <= 0:
            raise ValueError("Log writer limits must be positive")
        self.session_factory = session_factory
        self.queue_capacity = queue_capacity
        self.retained_limit = retained_limit
        self.batch_size = min(batch_size, queue_capacity)
        self.flush_interval = flush_interval
        self._queue: deque[dict] = deque()
        self._batch: list[dict] = []
        self._lock = threading.Lock()
        self._wake = asyncio.Event()
        self._stopping = False
        self._loop = None
        self.task: asyncio.Task | None = None
        self.handler = DatabaseLogHandler(self)
        self.persisted_count = 0
        self.failure_count = 0
        self.dropped_count = 0
        self.last_error: str | None = None

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue) + len(self._batch)

    def record_drop(self) -> None:
        with self._lock:
            self.dropped_count += 1

    def enqueue(self, event: dict) -> bool:
        with self._lock:
            if self._stopping or len(self._queue) + len(self._batch) >= self.queue_capacity:
                self.dropped_count += 1
                return False
            self._queue.append(event)
            wake = len(self._queue) >= self.batch_size
        if wake and self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._wake.set)
        return True

    def start(self) -> None:
        if self.task is not None:
            raise RuntimeError("System log writer can only start once")
        self._loop = asyncio.get_running_loop()
        logging.getLogger("app").addHandler(self.handler)
        self.task = asyncio.create_task(self._run(), name="system-log-writer")

    def snapshot(self) -> LoggingStatus:
        return LoggingStatus(
            queue_capacity=self.queue_capacity,
            pending_count=self.pending_count,
            dropped_count=self.dropped_count,
            persisted_count=self.persisted_count,
            failure_count=self.failure_count,
            last_error=self.last_error,
            retained_limit=self.retained_limit,
            writer_running=self.task is not None and not self.task.done(),
        )

    async def _persist(self, batch: list[dict]) -> None:
        async with self.session_factory.begin() as session:
            # Stable event IDs make a retry after an uncertain commit acknowledgement safe.
            existing = set(
                await session.scalars(
                    select(SystemLog.event_id).where(
                        SystemLog.event_id.in_([row["event_id"] for row in batch])
                    )
                )
            )
            session.add_all([SystemLog(**row) for row in batch if row["event_id"] not in existing])
            await session.flush()
            boundary = (
                await session.execute(
                    select(SystemLog.timestamp, SystemLog.id)
                    .order_by(SystemLog.timestamp.desc(), SystemLog.id.desc())
                    .offset(self.retained_limit)
                    .limit(1)
                )
            ).first()
            if boundary is not None:
                await session.execute(
                    delete(SystemLog).where(
                        or_(
                            SystemLog.timestamp < boundary.timestamp,
                            and_(
                                SystemLog.timestamp == boundary.timestamp,
                                SystemLog.id <= boundary.id,
                            ),
                        )
                    )
                )

    async def _run(self) -> None:
        while not self._stopping or self.pending_count:
            if not self._batch and not self._stopping and self.pending_count < self.batch_size:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.flush_interval)
                except TimeoutError:
                    pass
            self._wake.clear()
            with self._lock:
                if not self._batch:
                    self._batch = [
                        self._queue.popleft() for _ in range(min(self.batch_size, len(self._queue)))
                    ]
                batch = self._batch
            if not batch and self._stopping:
                break
            try:
                # Audit rows are inserted directly by user-management transactions.
                # Even at WARNING log level with an empty runtime queue, periodically
                # enforce retention so audit-only workloads cannot grow without bound.
                await self._persist(batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failure_count += 1
                self.last_error = type(exc).__name__
                if self._stopping:
                    break
                await asyncio.sleep(self.flush_interval)
            else:
                self.persisted_count += len(batch)
                self.last_error = None
                with self._lock:
                    self._batch = []

    async def stop(self, timeout: float = 2.0) -> bool:
        logging.getLogger("app").removeHandler(self.handler)
        self._stopping = True
        self._wake.set()
        if self.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=timeout)
            except TimeoutError:
                self.task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(self.task), timeout=1.0)
                except (TimeoutError, asyncio.CancelledError):
                    pass
        with self._lock:
            pending = len(self._queue) + len(self._batch)
            self.dropped_count += pending
            self._queue.clear()
            self._batch = []
        self.handler.close()
        return pending == 0
