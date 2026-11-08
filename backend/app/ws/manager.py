"""Per-connection backpressure and bounded, authenticated WebSocket delivery."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.realtime.events import SampleEvent
from app.services.auth import AuthService, Principal
from app.services.authorization import require_sample_reader

logger = logging.getLogger("app.websocket")


class _CloseConnection(Exception):
    def __init__(self, code: int, reason: str) -> None:
        self.code = code
        self.reason = reason


@dataclass(eq=False)
class _Connection:
    websocket: WebSocket
    token: str
    category: str | None
    queue: asyncio.Queue[dict[str, Any]]
    device_id: str | None = None
    identifier: str = field(default_factory=lambda: str(uuid4()))
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    close_code: int = 1000
    close_reason: str = "Connection ended"

    def request_close(self, code: int, reason: str) -> None:
        if not self.stop.is_set():
            self.close_code = code
            self.close_reason = reason
            self.stop.set()


class ConnectionManager:
    def __init__(
        self,
        auth_service: AuthService,
        *,
        queue_capacity: int = 100,
        send_timeout: float = 2.0,
        auth_check_interval: float = 1.0,
        max_connections: int = 100,
        enabled: bool = True,
    ) -> None:
        if min(queue_capacity, send_timeout, auth_check_interval) <= 0:
            raise ValueError("WebSocket limits must be positive")
        if max_connections < 0:
            raise ValueError("WebSocket connection limit must be non-negative")
        self.auth_service = auth_service
        self.queue_capacity = queue_capacity
        self.send_timeout = send_timeout
        self.auth_check_interval = auth_check_interval
        self.max_connections = max_connections
        self._connections: dict[str, _Connection] = {}
        self._enabled = enabled
        self._closing = False
        self._transition_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled and not self._closing

    @property
    def connection_count(self) -> int:
        return sum(not connection.stop.is_set() for connection in self._connections.values())

    async def serve(
        self,
        websocket: WebSocket,
        token: str,
        principal: Principal,
        category: str | None = None,
        device_id: str | None = None,
    ) -> None:
        require_sample_reader(principal)
        if not self.enabled:
            await self.reject_unavailable(websocket)
            return
        if self.max_connections > 0 and len(self._connections) >= self.max_connections:
            await self._safe_close(websocket, 1013, "Stream capacity reached")
            return

        connection = _Connection(
            websocket, token, category, asyncio.Queue(self.queue_capacity), device_id=device_id
        )
        connection.queue.put_nowait(
            {
                "type": "ready",
                "category": category,
                "device_id": device_id,
                "persistence": "pending",
            }
        )
        # Reserve before yielding so a concurrent disable also closes pending handshakes.
        self._connections[connection.identifier] = connection
        tasks: list[asyncio.Task] = []
        try:
            await asyncio.wait_for(websocket.accept(), timeout=self.send_timeout)
            tasks = [
                asyncio.create_task(self._sender(connection), name="websocket-sender"),
                asyncio.create_task(self._receiver(connection), name="websocket-receiver"),
                asyncio.create_task(connection.stop.wait(), name="websocket-stop"),
            ]
            completed, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            # Retrieve results for all completed tasks, including simultaneous failures.
            for task in completed:
                try:
                    task.result()
                except _CloseConnection as exc:
                    connection.request_close(exc.code, exc.reason)
                except (WebSocketDisconnect, OSError):
                    connection.request_close(1000, "Client disconnected")
        except (WebSocketDisconnect, OSError):
            connection.request_close(1000, "Client disconnected")
        except TimeoutError:
            connection.request_close(1013, "Connection timed out")
        except asyncio.CancelledError:
            connection.request_close(1001, "Server shutting down")
            raise
        except Exception:
            logger.error("WebSocket transport failed", exc_info=True)
            connection.request_close(1011, "Stream temporarily unavailable")
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._safe_close(websocket, connection.close_code, connection.close_reason)
            self._connections.pop(connection.identifier, None)
            connection.done.set()

    async def _authenticate(self, connection: _Connection) -> None:
        try:
            principal = await asyncio.wait_for(
                self.auth_service.authenticate(connection.token), timeout=self.send_timeout
            )
            require_sample_reader(principal)
        except (UnauthorizedError, ForbiddenError) as exc:
            raise _CloseConnection(1008, "Credentials expired or revoked") from exc
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _CloseConnection(1011, "Authentication temporarily unavailable") from exc

    async def _sender(self, connection: _Connection) -> None:
        while not connection.stop.is_set():
            try:
                message = await asyncio.wait_for(
                    connection.queue.get(), timeout=self.auth_check_interval
                )
            except TimeoutError:
                # Idle sockets must also close after logout, expiry or account disablement.
                await self._authenticate(connection)
                continue
            await self._authenticate(connection)
            if connection.stop.is_set():
                return
            try:
                await asyncio.wait_for(
                    connection.websocket.send_json(message), timeout=self.send_timeout
                )
            except TimeoutError as exc:
                raise _CloseConnection(1013, "Client is too slow") from exc

    async def _receiver(self, connection: _Connection) -> None:
        while not connection.stop.is_set():
            message = await connection.websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if text is None or len(text.encode("utf-8")) > 4096:
                raise _CloseConnection(1008, "Only small JSON ping messages are supported")
            try:
                payload = json.loads(text)
            except (ValueError, TypeError) as exc:
                raise _CloseConnection(1008, "Invalid JSON message") from exc
            if payload != {"type": "ping"}:
                raise _CloseConnection(1008, "Only ping messages are supported")
            self._offer(connection, {"type": "pong"})

    def _offer(self, connection: _Connection, payload: dict[str, Any]) -> None:
        if connection.stop.is_set():
            return
        try:
            connection.queue.put_nowait(payload)
        except asyncio.QueueFull:
            connection.request_close(1013, "Client event queue is full")

    async def publish(self, event: SampleEvent) -> None:
        """Offer an event without waiting on any socket or authentication database query."""
        payload = event.model_dump(mode="json")
        for connection in tuple(self._connections.values()):
            if (connection.category is None or connection.category == event.category) and (
                connection.device_id is None or connection.device_id == event.device_id
            ):
                self._offer(connection, payload)

    async def _safe_close(self, websocket: WebSocket, code: int, reason: str) -> None:
        try:
            await asyncio.wait_for(
                websocket.close(code=code, reason=reason), timeout=self.send_timeout
            )
        except (TimeoutError, RuntimeError, WebSocketDisconnect, OSError):
            pass

    async def reject_unavailable(self, websocket: WebSocket) -> None:
        """Keep temporary unavailability distinct from a rejected credential handshake."""
        reason = "Server shutting down" if self._closing else "WebSocket service disabled"
        extensions = getattr(websocket, "scope", {}).get("extensions", {})
        if "websocket.http.response" in extensions:
            response = JSONResponse(
                {"detail": reason}, status_code=503, headers={"Retry-After": "1"}
            )
            try:
                await asyncio.wait_for(
                    websocket.send_denial_response(response), timeout=self.send_timeout
                )
                return
            except (TimeoutError, RuntimeError, WebSocketDisconnect, OSError):
                pass
        await self._safe_close(websocket, 1001, reason)

    async def set_enabled(self, enabled: bool) -> None:
        """Serialize runtime switches; disabling drains sockets before a later enable."""
        async with self._transition_lock:
            if self._closing:
                return
            self._enabled = enabled
            if not enabled:
                await self._close_connections("WebSocket service disabled")

    async def close_all(self) -> None:
        # Shutdown takes effect before waiting for a pending runtime transition.
        self._closing = True
        async with self._transition_lock:
            await self._close_connections("Server shutting down")

    async def _close_connections(self, reason: str) -> None:
        connections = tuple(self._connections.values())
        for connection in connections:
            connection.request_close(1001, reason)
        if connections:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(connection.done.wait() for connection in connections)),
                    timeout=self.send_timeout * 2 + 1,
                )
            except TimeoutError:
                logger.warning("Some WebSocket handlers exceeded the shutdown deadline")
