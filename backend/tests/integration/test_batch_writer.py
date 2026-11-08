import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.models.realtime import RealtimeSample
from app.realtime import batch_writer
from app.realtime.batch_writer import BatchWriter
from app.services.monitoring import create_sample_event

pytestmark = pytest.mark.integration


def sample(value="81"):
    return create_sample_event(value=Decimal(value))


async def stored_samples(app):
    async with app.state.session_factory() as session:
        return list(await session.scalars(select(RealtimeSample).order_by(RealtimeSample.id)))


async def test_batch_persists_complete_snapshots_and_deduplicates_replay(app):
    events = [
        create_sample_event(
            value=Decimal(value), device_id=device_id, device_name=device_name, unit="°C"
        )
        for value, device_id, device_name in [
            ("80", "device-001", "設備 A"),
            ("80.000001", "device-002", "設備 B"),
        ]
    ]
    writer = BatchWriter(app.state.session_factory, batch_size=2, flush_interval=60)
    writer.start()
    for event in events:
        await writer.enqueue(event)
    assert await writer.stop(timeout=2)
    rows = await stored_samples(app)
    assert len(rows) == 2
    assert [row.event_id for row in rows] == [str(event.event_id) for event in events]
    assert [row.value for row in rows] == [Decimal("80"), Decimal("80.000001")]
    assert [row.is_anomaly for row in rows] == [False, True]
    assert all(row.timestamp.utcoffset().total_seconds() == 0 for row in rows)
    assert all(row.threshold == Decimal("80") for row in rows)
    assert [row.device_id for row in rows] == [event.device_id for event in events]
    assert [row.device_name for row in rows] == [event.device_name for event in events]
    assert [row.unit for row in rows] == ["°C", "°C"]

    replay = BatchWriter(app.state.session_factory, batch_size=10, flush_interval=60)
    replay.start()
    for event in [events[0], events[0], events[1]]:
        await replay.enqueue(event)
    assert await replay.stop(timeout=2)
    assert len(await stored_samples(app)) == 2
    assert replay.persisted_count == 0
    assert replay.pending_count == 0


async def test_transaction_failure_rolls_back_whole_batch_before_retry(app, monkeypatch):
    original_store = batch_writer.add_missing_samples
    attempts = 0
    rollback_seen = asyncio.Event()

    async def fail_after_flush(session, events):
        nonlocal attempts
        attempts += 1
        count = await original_store(session, events)
        if attempts == 1:
            rollback_seen.set()
            raise OperationalError(None, None, RuntimeError("temporary failure before commit"))
        return count

    monkeypatch.setattr(batch_writer, "add_missing_samples", fail_after_flush)
    writer = BatchWriter(
        app.state.session_factory, batch_size=2, retry_initial=0.05, retry_max=0.05
    )
    writer.start()
    events = [sample(), sample()]
    for event in events:
        await writer.enqueue(event)
    await asyncio.wait_for(rollback_seen.wait(), timeout=1)
    assert await writer.stop(timeout=2)
    assert attempts == 2
    assert writer.failure_count == 1
    assert writer.persisted_count == 2
    assert len(await stored_samples(app)) == 2


async def test_uncertain_commit_is_retried_without_duplicate_rows(app):
    class CommitThenLoseAcknowledgement:
        def __init__(self):
            self.attempts = 0

        @asynccontextmanager
        async def begin(self):
            self.attempts += 1
            async with app.state.session_factory.begin() as session:
                yield session
            if self.attempts == 1:
                raise OperationalError(
                    None,
                    None,
                    RuntimeError("commit acknowledgement lost"),
                    connection_invalidated=True,
                )

    transactions = CommitThenLoseAcknowledgement()
    writer = BatchWriter(transactions, batch_size=2, retry_initial=0.01, retry_max=0.01)
    writer.start()
    events = [sample(), sample()]
    for event in events:
        await writer.enqueue(event)
    assert await writer.stop(timeout=2)
    assert transactions.attempts == 2
    assert writer.failure_count == 1
    assert writer.pending_count == 0
    assert len(await stored_samples(app)) == 2
