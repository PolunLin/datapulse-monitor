"""Validate measurements and capture the threshold used for anomaly evaluation."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import TypeAdapter

from app.realtime.events import SampleEvent
from app.schemas.device import LEGACY_DEVICE_ID, LEGACY_DEVICE_NAME
from app.schemas.record import Measurement

measurement = TypeAdapter(Measurement)


def create_sample_event(
    *,
    value: Decimal,
    category: str = "temperature",
    threshold: Decimal = Decimal("80"),
    title: str | None = None,
    timestamp: datetime | None = None,
    event_id: UUID | None = None,
    device_id: str = LEGACY_DEVICE_ID,
    device_name: str = LEGACY_DEVICE_NAME,
    unit: str = "",
) -> SampleEvent:
    validated_value = measurement.validate_python(value)
    validated_threshold = measurement.validate_python(threshold)
    attributes = {}
    if timestamp is not None:
        attributes["timestamp"] = timestamp
    if event_id is not None:
        attributes["event_id"] = event_id
    return SampleEvent(
        device_id=device_id,
        device_name=device_name,
        unit=unit,
        title=title if title is not None else f"Simulated {category}",
        value=validated_value,
        category=category,
        threshold=validated_threshold,
        is_anomaly=validated_value > validated_threshold,
        **attributes,
    )
