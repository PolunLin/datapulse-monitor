"""One continuously running source of measurements from three simulated devices."""

import asyncio
import logging
import random
from decimal import Decimal

from app.realtime.devices import DeviceSpec, configured_devices
from app.realtime.dispatcher import EventDispatcher
from app.services.monitoring import create_sample_event

logger = logging.getLogger("app.realtime.simulator")
PRECISION = Decimal("0.000001")
MAX_MEASUREMENT = Decimal("999999999999.999999")


def _bounded_measurement(value: Decimal) -> Decimal:
    return min(MAX_MEASUREMENT, max(-MAX_MEASUREMENT, value)).quantize(PRECISION)


class Simulator:
    def __init__(
        self,
        dispatcher: EventDispatcher,
        *,
        interval: float = 1.0,
        category: str = "temperature",
        threshold: Decimal = Decimal("80"),
        random_seed: int | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("Simulator interval must be positive")
        self.dispatcher = dispatcher
        self.interval = interval
        self.category = category
        self.threshold = threshold
        self.devices = configured_devices(category, threshold)
        self.task: asyncio.Task[None] | None = None
        self.generated_count = 0
        self.last_error: str | None = None
        self._random = random.Random(random_seed)
        self._centers = {
            device.id: _bounded_measurement(threshold - Decimal(12 + index * 6))
            for index, device in enumerate(self.devices)
        }
        self._values = dict(self._centers)

    def _next_value(self, device: DeviceSpec) -> Decimal:
        # Gradual movement around each equipment's baseline makes separate traces useful.
        previous = self._values[device.id]
        movement = Decimal(self._random.randint(-800_000, 800_000)) / 1_000_000
        drift = (self._centers[device.id] - previous) * Decimal("0.05")
        baseline = _bounded_measurement(previous + movement + drift)
        self._values[device.id] = baseline
        if self._random.random() < 0.04:
            # A temporary over-temperature reading does not move the normal baseline.
            spike = Decimal(self._random.randint(3_000_000, 10_000_000)) / 1_000_000
            return _bounded_measurement(device.threshold + spike)
        return baseline

    @property
    def healthy(self) -> bool:
        return self.task is not None and not self.task.done() and self.last_error is None

    def start(self) -> None:
        if self.task is not None:
            raise RuntimeError("Simulator has already been started")
        self.task = asyncio.create_task(self._run(), name="realtime-simulator")
        self.task.add_done_callback(self._observe_completion)

    def _observe_completion(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _run(self) -> None:
        try:
            while True:
                for device in self.devices:
                    event = create_sample_event(
                        value=self._next_value(device),
                        category=device.category,
                        threshold=device.threshold,
                        title=f"{device.name} 溫度"
                        if device.category == "temperature"
                        else device.name,
                        device_id=device.id,
                        device_name=device.name,
                        unit=device.unit,
                    )
                    await self.dispatcher.publish(event)
                    self.generated_count += 1
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = type(exc).__name__
            logger.error("Simulator stopped after %s", self.last_error)
            raise
