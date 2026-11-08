"""Immutable event snapshots shared by delivery and persistence consumers."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.base import utc_now
from app.schemas.device import (
    LEGACY_DEVICE_ID,
    LEGACY_DEVICE_NAME,
    DeviceId,
    DeviceName,
    DeviceUnit,
)
from app.schemas.record import Category, Measurement, Title, UtcDatetime


class SampleEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    type: Literal["sample.created"] = "sample.created"
    event_id: UUID = Field(default_factory=uuid4)
    device_id: DeviceId = LEGACY_DEVICE_ID
    device_name: DeviceName = LEGACY_DEVICE_NAME
    unit: DeviceUnit = ""
    title: Title
    value: Measurement
    category: Category
    timestamp: UtcDatetime = Field(default_factory=utc_now)
    threshold: Measurement
    is_anomaly: bool
    persistence: Literal["pending"] = "pending"

    @model_validator(mode="after")
    def validate_anomaly(self) -> "SampleEvent":
        if self.is_anomaly != (self.value > self.threshold):
            raise ValueError("is_anomaly must reflect value > threshold")
        return self
