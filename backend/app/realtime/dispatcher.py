"""Fan out an accepted event without competing for the persistence queue."""

from collections.abc import Awaitable, Callable

from app.realtime.batch_writer import BatchWriter
from app.realtime.events import SampleEvent


class EventDispatcher:
    def __init__(
        self, writer: BatchWriter, publish: Callable[[SampleEvent], Awaitable[None]]
    ) -> None:
        self.writer = writer
        self._publish = publish

    async def publish(self, event: SampleEvent) -> None:
        # Applying backpressure before fan-out prevents announcing events that the
        # persistence path has not accepted. Delivery remains best effort.
        await self.writer.enqueue(event)
        await self._publish(event)
