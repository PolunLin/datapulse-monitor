"""Committed-data analytics; UTC half-open filters are also shared by exports."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.device import DeviceId
from app.schemas.record import Category, Title, UtcDatetime

DataSource = Literal["records", "realtime"]
BucketInterval = Literal["hour", "day"]


class AnalyticsFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: DataSource = "records"
    category: Category | None = None
    q: Title | None = None
    created_by: int | None = Field(default=None, ge=1)
    device_id: DeviceId | None = None
    event_id: UUID | None = None
    is_anomaly: bool | None = None
    start_time: UtcDatetime | None = None
    end_time: UtcDatetime | None = None
    interval: BucketInterval = "day"

    @model_validator(mode="after")
    def validate_filters(self) -> "AnalyticsFilters":
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValueError("start_time must be earlier than end_time")
        if self.source == "realtime" and self.created_by is not None:
            raise ValueError("created_by is available only for the records source")
        if self.source != "realtime" and self.device_id is not None:
            raise ValueError("device_id is available only for the realtime source")
        if self.source != "realtime" and (self.event_id is not None or self.is_anomaly is not None):
            raise ValueError("event_id and is_anomaly are available only for the realtime source")
        return self


class AggregateStatistics(BaseModel):
    count: int
    total: Decimal
    average: Decimal | None
    minimum: Decimal | None
    maximum: Decimal | None


class CategoryStatistics(AggregateStatistics):
    category: str


class TrendStatistics(AggregateStatistics):
    timestamp: datetime


class AnalyticsResult(BaseModel):
    source: DataSource
    interval: BucketInterval
    summary: AggregateStatistics
    categories: list[CategoryStatistics]
    trend: list[TrendStatistics]
