from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from app.db.base import utc_now
from app.schemas.device import SimulatedDeviceId

Title = Annotated[str, Field(min_length=1, max_length=200)]
Category = Annotated[str, Field(min_length=1, max_length=100)]
Measurement = Annotated[Decimal, Field(max_digits=18, decimal_places=6, allow_inf_nan=False)]


def normalize_utc(value: datetime) -> datetime:
    try:
        normalized = value.astimezone(UTC)
    except OverflowError as exc:
        raise ValueError("Timestamp is outside the supported UTC range") from exc
    if normalized.year < 1000:
        raise ValueError("Timestamp must be on or after 1000-01-01T00:00:00Z")
    return normalized


def parse_timestamp(value: object) -> datetime:
    # Prevent numeric CSV/JSON values from being silently interpreted as Unix time.
    # The input contract is an explicit zoned ISO timestamp (or an internal datetime).
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError("Use an ISO 8601 timestamp with an explicit timezone")


UtcDatetime = Annotated[
    AwareDatetime, BeforeValidator(parse_timestamp), AfterValidator(normalize_utc)
]


class RecordCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: Title
    value: Measurement
    category: SimulatedDeviceId
    timestamp: UtcDatetime = Field(default_factory=utc_now)


class RecordUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: Title | None = None
    value: Measurement | None = None
    category: SimulatedDeviceId | None = None
    timestamp: UtcDatetime | None = None

    @model_validator(mode="after")
    def require_changes(self) -> "RecordUpdate":
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Updated fields cannot be null")
        return self


class RecordRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    value: Decimal
    category: str
    timestamp: datetime
    created_by: int
    created_at: datetime
    updated_at: datetime


class RecordFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    category: Category | None = None
    q: Title | None = None
    created_by: int | None = Field(default=None, ge=1)
    start_time: UtcDatetime | None = None
    end_time: UtcDatetime | None = None
    sort_by: Literal["timestamp", "value", "title", "id"] = "timestamp"
    sort_order: Literal["asc", "desc"] = "desc"

    @model_validator(mode="after")
    def validate_time_range(self) -> "RecordFilters":
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValueError("start_time must be earlier than end_time")
        return self


class RecordPage(BaseModel):
    items: list[RecordRead]
    total: int
    page: int
    page_size: int
