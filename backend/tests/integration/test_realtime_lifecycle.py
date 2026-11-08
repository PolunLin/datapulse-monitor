"""Exercise actual application lifecycle against a disposable MariaDB schema."""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.db.session import SessionFactory, create_database
from app.main import create_app
from app.models.realtime import RealtimeSample

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def sample_store(settings) -> AsyncIterator[SessionFactory]:
    # The shared settings fixture requires a database name ending in _test.
    engine, session_factory = create_database(settings)
    async with session_factory.begin() as session:
        await session.execute(delete(RealtimeSample))
    try:
        yield session_factory
    finally:
        async with session_factory.begin() as session:
            await session.execute(delete(RealtimeSample))
        await engine.dispose()


async def count_samples(session_factory: SessionFactory) -> int:
    async with session_factory() as session:
        return await session.scalar(select(func.count(RealtimeSample.id))) or 0


async def test_lifespan_generates_each_device_without_subscribers_and_drains_partial_batch(
    settings, sample_store
):
    configured = settings.model_copy(
        update={
            "simulator_enabled": True,
            "simulator_interval_seconds": 0.02,
            "realtime_batch_size": 1000,
            "realtime_flush_interval_seconds": 60.0,
            "realtime_shutdown_timeout_seconds": 2.0,
        }
    )
    application = create_app(configured)
    async with application.router.lifespan_context(application):
        simulator = application.state.simulator
        assert simulator is not None
        async with asyncio.timeout(3):
            while simulator.generated_count < 6:
                await asyncio.sleep(0.01)
        assert application.state.ws_manager.connection_count == 0
        assert await count_samples(sample_store) == 0
        assert application.state.batch_writer.pending_count >= 6
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://testserver"
        ) as client:
            assert (await client.get("/health/ready")).status_code == 200

    writer = application.state.batch_writer
    assert simulator.task.done()
    assert writer.task.done()
    assert writer.pending_count == 0
    assert await count_samples(sample_store) == simulator.generated_count
    assert writer.persisted_count == simulator.generated_count
    assert application.state.ws_manager.connection_count == 0
    async with sample_store() as session:
        rows = list(await session.scalars(select(RealtimeSample)))
    counts = Counter(row.device_id for row in rows)
    assert set(counts) == {"device-001", "device-002", "device-003"}
    assert all(count >= 2 for count in counts.values())
    assert {row.device_name for row in rows} == {"設備 A", "設備 B", "設備 C"}
    assert all(row.category == "temperature" and row.unit == "°C" for row in rows)


async def test_disabled_simulator_keeps_writer_available_without_generating(settings, sample_store):
    application = create_app(settings.model_copy(update={"simulator_enabled": False}))
    async with application.router.lifespan_context(application):
        assert application.state.simulator is None
        writer = application.state.batch_writer
        assert writer.task is not None and not writer.task.done()
        assert writer.pending_count == 0
        assert await count_samples(sample_store) == 0
    assert writer.task.done()
    assert await count_samples(sample_store) == 0


async def test_readiness_detects_stopped_writer_and_shutdown_still_cleans_up(
    settings, sample_store
):
    application = create_app(settings.model_copy(update={"simulator_enabled": False}))
    async with application.router.lifespan_context(application):
        writer = application.state.batch_writer
        writer.task.cancel()
        await asyncio.gather(writer.task, return_exceptions=True)
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://testserver"
        ) as client:
            assert (await client.get("/health/live")).status_code == 200
            response = await client.get("/health/ready")
            assert response.status_code == 503
            assert response.json()["status"] == "degraded"
    assert writer.task.done()
    assert application.state.ws_manager.connection_count == 0
    assert await count_samples(sample_store) == 0
