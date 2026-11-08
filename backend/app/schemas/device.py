"""Shared device metadata and backwards-compatible identity for older samples."""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

LEGACY_DEVICE_ID = "legacy"
LEGACY_DEVICE_NAME = "舊版未指定設備"
DeviceId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
DeviceName = Annotated[str, Field(min_length=1, max_length=100)]
DeviceUnit = Annotated[str, Field(max_length=16)]


class SimulatedDeviceId(StrEnum):
    A = "device-001"
    B = "device-002"
    C = "device-003"


SIMULATED_DEVICE_NAMES = {
    SimulatedDeviceId.A: "設備 A",
    SimulatedDeviceId.B: "設備 B",
    SimulatedDeviceId.C: "設備 C",
}


class DeviceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: DeviceId
    name: DeviceName
    category: str | None
    unit: DeviceUnit
    threshold: Decimal | None


class DeviceCatalog(BaseModel):
    items: list[DeviceRead]
    sample_interval_seconds: float = Field(gt=0, allow_inf_nan=False)
