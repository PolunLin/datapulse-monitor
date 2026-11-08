from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.device import LEGACY_DEVICE_ID, LEGACY_DEVICE_NAME, DeviceId
from app.schemas.record import Category, Title, UtcDatetime


class SampleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_id: UUID
    device_id: str = LEGACY_DEVICE_ID
    device_name: str = LEGACY_DEVICE_NAME
    unit: str = ""
    title: str
    value: Decimal
    category: str
    timestamp: datetime
    threshold: Decimal
    is_anomaly: bool
    created_at: datetime


class SampleFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    category: Category | None = None
    q: Title | None = None
    device_id: DeviceId | None = None
    event_id: UUID | None = None
    is_anomaly: bool | None = None
    start_time: UtcDatetime | None = None
    end_time: UtcDatetime | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "SampleFilters":
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValueError("start_time must be earlier than end_time")
        return self


class MonitoringSampleFilters(SampleFilters):
    page_size: int = Field(default=300, ge=1, le=300)


class SampleFeedFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    device_id: DeviceId
    after_id: int | None = Field(default=None, ge=0)
    limit: int = Field(default=300, ge=1, le=300)


class SampleFeed(BaseModel):
    items: list[SampleRead]
    next_cursor: int = Field(ge=0)
    has_more: bool


class SamplePage(BaseModel):
    items: list[SampleRead]
    total: int
    page: int
    page_size: int


class MonitoringStatus(BaseModel):
    simulator_enabled: bool
    generator_running: bool
    generated_count: int
    writer_running: bool
    writer_healthy: bool
    pending_count: int
    persisted_count: int
    failure_count: int
    last_error: str | None
    websocket_enabled: bool
    websocket_connections: int
    batch_size: int
    flush_interval_seconds: float


class WebSocketControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(strict=True)


class WebSocketStatus(BaseModel):
    enabled: bool
    connections: int = Field(ge=0)
