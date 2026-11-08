from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.record import UtcDatetime


class LogFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None
    kind: Literal["request", "runtime", "audit"] | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=64)
    actor_id: int | None = Field(default=None, ge=1)
    q: str | None = Field(default=None, min_length=1, max_length=200)
    start_time: UtcDatetime | None = None
    end_time: UtcDatetime | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "LogFilters":
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValueError("start_time must be earlier than end_time")
        return self


class LogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    timestamp: datetime
    level: str
    kind: str
    logger: str
    message: str
    request_id: str | None
    method: str | None
    path: str | None
    status_code: int | None
    duration_ms: float | None
    error_type: str | None
    actor_id: int | None
    target_user_id: int | None
    action: str | None
    changes: dict[str, Any] | None


class LogPage(BaseModel):
    items: list[LogRead]
    total: int
    page: int
    page_size: int


class LoggingStatus(BaseModel):
    queue_capacity: int
    pending_count: int
    dropped_count: int
    persisted_count: int
    failure_count: int
    last_error: str | None
    retained_limit: int
    writer_running: bool


class DatabaseCounts(BaseModel):
    users: int
    active_users: int
    records: int
    realtime_samples: int
    system_logs: int


class PoolStatus(BaseModel):
    size: int
    checked_in: int
    checked_out: int
    overflow: int
    max_overflow: int
    timeout_seconds: float


class DatabaseStatus(BaseModel):
    status: Literal["healthy", "unavailable"]
    checked_at: datetime
    latency_ms: float
    server_version: str | None
    counts: DatabaseCounts | None
    latest_sample_at: datetime | None
    pool: PoolStatus
    error: str | None
