import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError

from app.realtime import batch_writer
from app.realtime.batch_writer import BatchWriter
from app.realtime.dispatcher import EventDispatcher
from app.realtime.events import SampleEvent
from app.realtime.generator import Simulator
from app.services.monitoring import create_sample_event


class Transactions:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    @asynccontextmanager
    async def begin(self):
        try:
            yield object()
        except Exception:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1


async def until(predicate, timeout=1.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


def sample(value="81"):
    return create_sample_event(value=Decimal(value))


@pytest.fixture
def storage(monkeypatch):
    batches = []

    async def store(_session, events):
        batches.append(tuple(events))
        return len({event.event_id for event in events})

    monkeypatch.setattr(batch_writer, "add_missing_samples", store)
    return batches


def test_event_validation_and_threshold_snapshot():
    assert not sample("80").is_anomaly
    event = sample("80.000001")
    assert event.is_anomaly
    assert event.model_dump(mode="json")["persistence"] == "pending"
    assert event.model_dump(mode="json")["value"] == "80.000001"
    with pytest.raises(ValidationError):
        event.value = Decimal("1")
    with pytest.raises(ValidationError):
        SampleEvent(**{**event.model_dump(), "is_anomaly": False})
    with pytest.raises(ValidationError):
        sample("NaN")


async def test_flush_when_batch_size_is_reached(storage):
    transactions = Transactions()
    writer = BatchWriter(transactions, batch_size=2, flush_interval=60)
    writer.start()
    events = [sample("1"), sample("2")]
    for event in events:
        await writer.enqueue(event)
    await until(lambda: writer.persisted_count == 2)
    assert storage == [tuple(events)]
    assert transactions.commits == 1
    assert writer.pending_count == 0
    assert writer.healthy
    assert await writer.stop()


async def test_flush_interval_persists_partial_batch(storage):
    writer = BatchWriter(Transactions(), batch_size=10, flush_interval=0.02)
    writer.start()
    event = sample()
    await writer.enqueue(event)
    await until(lambda: writer.persisted_count == 1)
    assert storage == [(event,)]
    assert await writer.stop()


async def test_shutdown_flushes_without_waiting_for_interval(storage):
    writer = BatchWriter(Transactions(), batch_size=100, flush_interval=60)
    writer.start()
    events = [sample(), sample()]
    for event in events:
        await writer.enqueue(event)
    assert await writer.stop(timeout=1)
    assert storage == [tuple(events)]
    assert writer.pending_count == 0
    with pytest.raises(RuntimeError, match="not accepting"):
        await writer.enqueue(sample())


async def test_retry_retains_batch_until_transaction_commits(monkeypatch):
    transactions = Transactions()
    attempts = []

    async def store(_session, events):
        attempts.append(tuple(events))
        if len(attempts) < 3:
            raise OperationalError(None, None, RuntimeError("temporary outage"))
        return len(events)

    monkeypatch.setattr(batch_writer, "add_missing_samples", store)
    writer = BatchWriter(transactions, batch_size=1, retry_initial=0.001, retry_max=0.002)
    writer.start()
    event = sample()
    await writer.enqueue(event)
    await until(lambda: writer.persisted_count == 1)
    assert attempts == [(event,)] * 3
    assert transactions.rollbacks == 2
    assert transactions.commits == 1
    assert writer.failure_count == 2
    assert writer.healthy
    assert writer.last_error is None
    assert await writer.stop()


async def test_persistent_outage_has_bounded_shutdown(monkeypatch):
    async def unavailable(_session, _events):
        raise OperationalError(None, None, RuntimeError("private error detail"))

    monkeypatch.setattr(batch_writer, "add_missing_samples", unavailable)
    writer = BatchWriter(Transactions(), batch_size=1, retry_initial=0.01, retry_max=0.02)
    writer.start()
    await writer.enqueue(sample())
    await until(lambda: writer.failure_count > 0)
    assert not writer.healthy
    assert writer.last_error == "OperationalError"
    assert not await writer.stop(timeout=0.03)
    assert writer.pending_count == 1
    assert writer.last_error == "shutdown_timeout"
    assert writer.task.done()
    assert not await writer.stop(timeout=0.01)


async def test_backpressure_blocks_only_until_capacity_is_available(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def store(_session, events):
        entered.set()
        await release.wait()
        return len(events)

    monkeypatch.setattr(batch_writer, "add_missing_samples", store)
    writer = BatchWriter(Transactions(), batch_size=1, queue_capacity=1)
    writer.start()
    await writer.enqueue(sample())
    await asyncio.wait_for(entered.wait(), timeout=1)
    await writer.enqueue(sample())
    blocked = asyncio.create_task(writer.enqueue(sample()))
    await asyncio.sleep(0)
    assert not blocked.done()
    assert writer.pending_count == 2  # One in-flight batch plus the bounded queue.
    release.set()
    await asyncio.wait_for(blocked, timeout=1)
    assert await writer.stop(timeout=1)
    assert writer.persisted_count == 3


async def test_fatal_failure_releases_blocked_producer(monkeypatch):
    entered, fail = asyncio.Event(), asyncio.Event()

    async def store(_session, _events):
        entered.set()
        await fail.wait()
        raise IntegrityError(None, None, RuntimeError("permanent constraint failure"))

    monkeypatch.setattr(batch_writer, "add_missing_samples", store)
    writer = BatchWriter(Transactions(), batch_size=1, queue_capacity=1)
    writer.start()
    await writer.enqueue(sample())
    await asyncio.wait_for(entered.wait(), timeout=1)
    await writer.enqueue(sample())
    blocked = asyncio.create_task(writer.enqueue(sample()))
    await asyncio.sleep(0)
    assert not blocked.done()
    fail.set()
    with pytest.raises(RuntimeError, match="not accepting"):
        await asyncio.wait_for(blocked, timeout=1)
    assert not await writer.stop(timeout=1)
    assert writer.pending_count == 2
    assert writer.failure_count == 1
    assert writer.last_error == "IntegrityError"


async def test_external_writer_cancellation_does_not_cancel_shutdown_caller(monkeypatch):
    entered = asyncio.Event()

    async def blocked_store(_session, _events):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(batch_writer, "add_missing_samples", blocked_store)
    writer = BatchWriter(Transactions(), batch_size=1)
    writer.start()
    await writer.enqueue(sample())
    await asyncio.wait_for(entered.wait(), timeout=1)
    shutdown = asyncio.create_task(writer.stop(timeout=1))
    await asyncio.sleep(0)
    writer.task.cancel()
    assert not await asyncio.wait_for(shutdown, timeout=1)
    assert not await writer.stop(timeout=1)
    assert writer.pending_count == 1
    assert writer.last_error == "writer_cancelled"
    with pytest.raises(RuntimeError, match="not accepting"):
        await writer.enqueue(sample())


async def test_shutdown_deadline_includes_bounded_driver_cancellation_cleanup(monkeypatch):
    storing = asyncio.Event()
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()

    class SlowCancellationTransactions:
        @asynccontextmanager
        async def begin(self):
            try:
                yield object()
            finally:
                cleanup_entered.set()
                while not release_cleanup.is_set():
                    try:
                        await release_cleanup.wait()
                    except asyncio.CancelledError:
                        # Model a driver stuck in cancellation-resistant cleanup.
                        continue

    async def blocked_store(_session, _events):
        storing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(batch_writer, "add_missing_samples", blocked_store)
    writer = BatchWriter(SlowCancellationTransactions(), batch_size=1)
    writer.start()
    try:
        await writer.enqueue(sample())
        await asyncio.wait_for(storing.wait(), timeout=1)
        assert not await asyncio.wait_for(writer.stop(timeout=0.02), timeout=0.5)
        assert cleanup_entered.is_set()
        assert not writer.task.done()
        assert not writer.healthy
        assert writer.pending_count == 1
        assert writer.last_error == "shutdown_timeout"
    finally:
        release_cleanup.set()
        await asyncio.wait_for(asyncio.gather(writer.task, return_exceptions=True), timeout=1)


async def test_dispatcher_fans_out_the_same_accepted_event(storage):
    delivered = []

    async def publish(event):
        delivered.append(event)

    writer = BatchWriter(Transactions(), flush_interval=60)
    writer.start()
    dispatcher = EventDispatcher(writer, publish)
    event = sample()
    await dispatcher.publish(event)
    assert delivered == [event]
    assert writer.pending_count == 1
    assert await writer.stop()
    assert storage == [(event,)]
    with pytest.raises(RuntimeError):
        await dispatcher.publish(sample())
    assert delivered == [event]


async def test_seeded_simulator_is_repeatable_and_stops():
    async def capture():
        events = []
        reached = asyncio.Event()

        class Dispatcher:
            async def publish(self, event):
                events.append(event)
                if len(events) >= 6:
                    reached.set()

        simulator = Simulator(Dispatcher(), interval=0.001, random_seed=42)
        simulator.start()
        await asyncio.wait_for(reached.wait(), timeout=1)
        await simulator.stop()
        count = len(events)
        await asyncio.sleep(0.005)
        assert len(events) == count == simulator.generated_count
        assert simulator.task.done()
        return events[:6]

    first, second = await capture(), await capture()
    assert [event.value for event in first] == [event.value for event in second]
    assert len({event.event_id for event in first}) == 6
    assert [event.device_id for event in first] == ["device-001", "device-002", "device-003"] * 2
    assert [event.device_name for event in first] == ["設備 A", "設備 B", "設備 C"] * 2
    assert all(event.category == "temperature" and event.unit == "°C" for event in first)
    assert all(Decimal("0") <= event.value <= Decimal("100") for event in first)
