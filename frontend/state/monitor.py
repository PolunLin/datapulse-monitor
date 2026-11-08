"""Bounded session-local display buffer, independent of Streamlit's script runtime."""

from collections import deque
from collections.abc import Iterable

from clients.realtime import SampleEvent


class MonitorBuffer:
    def __init__(self, capacity: int = 300) -> None:
        if capacity < 1:
            raise ValueError("Capacity must be positive")
        self.capacity = capacity
        self._events: deque[SampleEvent] = deque()
        self._ids: set[str] = set()
        self.received_count = 0

    def append(self, events: Iterable[SampleEvent]) -> None:
        combined = {event.event_id: event for event in self._events}
        for event in events:
            previous = combined.get(event.event_id)
            if previous is None:
                self.received_count += 1
            # A later WS copy must not downgrade an already committed sample.
            if previous is None or previous.persistence != "committed":
                combined[event.event_id] = event
        retained = sorted(combined.values(), key=lambda event: (event.timestamp, event.event_id))[
            -self.capacity :
        ]
        self._events = deque(retained)
        self._ids = {event.event_id for event in retained}

    def recent(self, limit: int = 120) -> list[SampleEvent]:
        return list(self._events)[-max(1, min(limit, self.capacity)) :]

    def clear(self) -> None:
        self._events.clear()
        self._ids.clear()
        self.received_count = 0
