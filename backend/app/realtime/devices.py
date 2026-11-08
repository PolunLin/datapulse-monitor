"""The fixed equipment catalog used by both the simulator and monitoring API."""

from dataclasses import dataclass
from decimal import Decimal

from app.schemas.device import SIMULATED_DEVICE_NAMES


@dataclass(frozen=True)
class DeviceSpec:
    id: str
    name: str
    category: str
    unit: str
    threshold: Decimal


def configured_devices(
    category: str = "temperature", threshold: Decimal = Decimal("80")
) -> tuple[DeviceSpec, ...]:
    return tuple(
        DeviceSpec(
            id=identifier.value,
            name=name,
            category=category,
            unit="°C" if category == "temperature" else "",
            threshold=threshold,
        )
        for identifier, name in SIMULATED_DEVICE_NAMES.items()
    )
