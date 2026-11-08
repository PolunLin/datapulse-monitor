"""One leased WebSocket receiver per UI session; background threads never touch UI state."""

import json
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
from websockets.sync.client import connect


@dataclass(frozen=True, slots=True)
class SampleEvent:
    event_id: str
    title: str
    value: Decimal
    category: str
    timestamp: datetime
    threshold: Decimal
    is_anomaly: bool
    persistence: Literal["pending", "committed"] = "pending"
    device_id: str = "legacy"
    device_name: str = "舊版未指定設備"
    unit: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "title": self.title,
            "value": str(self.value),
            "category": self.category,
            "timestamp": self.timestamp.isoformat(),
            "threshold": str(self.threshold),
            "is_anomaly": self.is_anomaly,
            "persistence": self.persistence,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "unit": self.unit,
        }


def _measurement(value: Any) -> Decimal:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValueError("Invalid measurement")
    try:
        decimal = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid measurement") from None
    if not decimal.is_finite() or decimal.adjusted() > 11 or decimal.as_tuple().exponent < -6:
        raise ValueError("Invalid measurement")
    return decimal


def _parse_sample(payload: dict, persistence: Literal["pending", "committed"]) -> SampleEvent:
    try:
        identifier = str(UUID(payload["event_id"]))
        timestamp = datetime.fromisoformat(payload["timestamp"])
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Timestamp requires an offset")
        timestamp = timestamp.astimezone(UTC)
        if timestamp.year < 1000:
            raise ValueError("Timestamp is out of range")
        title, category = payload["title"], payload["category"]
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
            raise ValueError("Invalid title")
        if not isinstance(category, str) or not 1 <= len(category.strip()) <= 100:
            raise ValueError("Invalid category")
        value, threshold = _measurement(payload["value"]), _measurement(payload["threshold"])
        anomaly = payload["is_anomaly"]
        if type(anomaly) is not bool or anomaly != (value > threshold):
            raise ValueError("Invalid anomaly marker")
        device_id = payload.get("device_id", "legacy")
        device_name = payload.get("device_name", "舊版未指定設備")
        unit = payload.get("unit", "")
        if not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", device_id):
            raise ValueError("Invalid device ID")
        if not isinstance(device_name, str) or not 1 <= len(device_name.strip()) <= 100:
            raise ValueError("Invalid device name")
        if not isinstance(unit, str) or len(unit) > 16:
            raise ValueError("Invalid unit")
        return SampleEvent(
            identifier,
            title,
            value,
            category,
            timestamp,
            threshold,
            anomaly,
            persistence,
            device_id,
            device_name,
            unit,
        )
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError("Invalid sample event") from exc


def parse_sample(payload: Any) -> SampleEvent:
    """WebSocket messages must keep the explicit pending protocol marker."""
    if not isinstance(payload, dict) or payload.get("type") != "sample.created":
        raise ValueError("Not a sample event")
    if payload.get("persistence") != "pending":
        raise ValueError("Invalid persistence marker")
    return _parse_sample(payload, "pending")


def parse_stored_sample(payload: Any) -> SampleEvent:
    """REST rows with a database ID are committed, without weakening WS validation."""
    if not isinstance(payload, dict) or type(payload.get("id")) is not int or payload["id"] < 1:
        raise ValueError("Invalid stored sample")
    if payload.get("persistence") not in {None, "committed"}:
        raise ValueError("Invalid stored persistence marker")
    return _parse_sample(payload, "committed")


@dataclass(frozen=True, slots=True)
class ReceiverSnapshot:
    status: str
    message: str
    received_count: int
    dropped_count: int
    reconnect_count: int
    invalid_count: int
    last_event_at: datetime | None


_MESSAGES = {
    "connecting": "正在連接即時資料來源…",
    "connected": "即時連線正常。",
    "reconnecting": "即時連線中斷，正在重新連接；已落庫的最近資料會由歷史同步補入。",
    "stopped": "即時連線已停止。",
    "auth_failed": "即時連線授權失效或被拒絕，請重新登入後再試。",
    "error": "即時資料格式或連線設定不正確，請稍後重新連接。",
}


class RealtimeReceiver:
    """Bounded sync receiver with cancellation, retry backoff and an idle lease.

    Call ``touch`` from each monitor fragment rerun. If the browser disconnects
    and reruns cease, the lease closes the socket without private Streamlit APIs.
    ``start`` is idempotent; a stopped receiver is intentionally not restartable.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        category: str | None = None,
        device_id: str | None = None,
        queue_capacity: int = 300,
        idle_timeout: float = 30.0,
        retry_initial: float = 0.5,
        retry_max: float = 8.0,
        connect_timeout: float = 3.0,
        receive_timeout: float = 0.25,
    ) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"ws", "wss"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("WebSocket URL must be ws/wss without credentials or query")
        if not isinstance(token, str) or not token:
            raise ValueError("A token is required")
        if category is not None:
            category = category.strip()
            if not 1 <= len(category) <= 100:
                raise ValueError("Category must contain 1 to 100 characters")
        if device_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", device_id):
            raise ValueError("Invalid device ID")
        limits = (idle_timeout, retry_initial, retry_max, connect_timeout, receive_timeout)
        if (
            type(queue_capacity) is not int
            or queue_capacity < 1
            or any(not math.isfinite(value) or value <= 0 for value in limits)
            or retry_max < retry_initial
        ):
            raise ValueError("Receiver limits must be finite and positive")
        query = {
            key: value
            for key, value in {"category": category, "device_id": device_id}.items()
            if value is not None
        }
        self.url = url + ("?" + urlencode(query) if query else "")
        self.category = category
        self.device_id = device_id
        self._token = token
        self._queue: deque[SampleEvent] = deque(maxlen=queue_capacity)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_touch = time.monotonic()
        self._idle_timeout = idle_timeout
        self._retry_initial = retry_initial
        self._retry_max = retry_max
        self._connect_timeout = connect_timeout
        self._receive_timeout = receive_timeout
        self._status = "stopped"
        self._received_count = 0
        self._dropped_count = 0
        self._reconnect_count = 0
        self._invalid_count = 0
        self._last_event_at: datetime | None = None

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._last_touch = time.monotonic()
            self._status = "connecting"
            self._thread = threading.Thread(
                target=self._run, name="datapulse-receiver", daemon=True
            )
            self._thread.start()

    def touch(self) -> None:
        with self._lock:
            self._last_touch = time.monotonic()

    def stop(self, timeout: float = 2.0) -> bool:
        """Request closing; return whether the worker has finished within the bound."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0, timeout))
        return not self.is_alive

    def snapshot(self) -> ReceiverSnapshot:
        with self._lock:
            return ReceiverSnapshot(
                self._status,
                _MESSAGES[self._status],
                self._received_count,
                self._dropped_count,
                self._reconnect_count,
                self._invalid_count,
                self._last_event_at,
            )

    def drain(self) -> list[SampleEvent]:
        with self._lock:
            events = list(self._queue)
            self._queue.clear()
            return events

    def _set_status(self, status: str) -> None:
        with self._lock:
            self._status = status
            if status == "auth_failed":
                self._queue.clear()

    def _expired(self) -> bool:
        with self._lock:
            return time.monotonic() - self._last_touch >= self._idle_timeout

    def _offer(self, event: SampleEvent) -> None:
        with self._lock:
            if len(self._queue) == self._queue.maxlen:
                self._dropped_count += 1
            self._queue.append(event)
            self._received_count += 1
            self._last_event_at = datetime.now(UTC)

    def _wait_retry(self, delay: float) -> bool:
        deadline = time.monotonic() + delay
        while not self._stop.is_set() and not self._expired():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            self._stop.wait(min(remaining, self._receive_timeout))
        return False

    def _receive(self, websocket) -> None:
        while not self._stop.is_set() and not self._expired():
            try:
                message = websocket.recv(timeout=self._receive_timeout)
            except TimeoutError:
                continue
            if not isinstance(message, str):
                raise ValueError("Expected a JSON text message")
            payload = json.loads(message)
            if not isinstance(payload, dict):
                raise ValueError("Expected an object")
            if payload.get("type") == "ready":
                self._set_status("connected")
                continue
            if payload == {"type": "pong"}:
                continue
            event = parse_sample(payload)
            if self.category is not None and event.category != self.category:
                raise ValueError("Unexpected category")
            if self.device_id is not None and event.device_id != self.device_id:
                raise ValueError("Unexpected device")
            self._set_status("connected")
            self._offer(event)

    def _run(self) -> None:
        delay = self._retry_initial
        terminal = False
        try:
            while not self._stop.is_set() and not self._expired():
                websocket = None
                try:
                    websocket = connect(
                        self.url,
                        additional_headers={"Authorization": f"Bearer {self._token}"},
                        proxy=None,
                        open_timeout=self._connect_timeout,
                        close_timeout=0.5,
                        max_size=4096,
                        max_queue=16,
                        ping_interval=20,
                        ping_timeout=20,
                    )
                    self._receive(websocket)
                    break
                except InvalidStatus as exc:
                    if exc.response.status_code in {401, 403}:
                        self._set_status("auth_failed")
                        terminal = True
                        break
                except ConnectionClosed as exc:
                    if exc.rcvd is not None and exc.rcvd.code == 1008:
                        self._set_status("auth_failed")
                        terminal = True
                        break
                except (OSError, TimeoutError, InvalidHandshake):
                    pass
                except (ValueError, TypeError, OverflowError):
                    with self._lock:
                        self._invalid_count += 1
                    self._set_status("error")
                    terminal = True
                    break
                except Exception:
                    self._set_status("error")
                    terminal = True
                    break
                finally:
                    if websocket is not None:
                        try:
                            websocket.close()
                        except Exception:
                            pass
                if self._stop.is_set() or self._expired():
                    break
                with self._lock:
                    self._reconnect_count += 1
                self._set_status("reconnecting")
                if not self._wait_retry(delay):
                    break
                delay = min(delay * 2, self._retry_max)
        finally:
            if not terminal:
                self._set_status("stopped")
            self._token = ""
