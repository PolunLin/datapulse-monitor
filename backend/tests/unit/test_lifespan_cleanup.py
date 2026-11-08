"""Cancellation during shutdown still releases owned tasks, sockets and the Engine."""

import asyncio
import importlib

import pytest
from fastapi import FastAPI

from app.core.config import Settings

lifecycle = importlib.import_module("app.lifespan")


async def test_cancelling_lifespan_waits_for_cleanup_then_propagates(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    disposed = []
    closed = []

    class Engine:
        async def dispose(self):
            disposed.append(True)

    class Writer:
        task = None
        pending_count = 0

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            self.task = asyncio.create_task(asyncio.Event().wait())

        async def stop(self, timeout):
            entered.set()
            await release.wait()
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            return True

    class Manager:
        def __init__(self, *_args, **_kwargs):
            pass

        async def publish(self, _event):
            pass

        async def close_all(self):
            closed.append(True)

    class LogWriter:
        def __init__(self, *_args, **_kwargs):
            self.task = None

        def start(self):
            self.task = asyncio.create_task(asyncio.Event().wait())

        async def stop(self, timeout):
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            return True

    monkeypatch.setattr(lifecycle, "create_database", lambda settings: (Engine(), object()))
    monkeypatch.setattr(lifecycle, "BatchWriter", Writer)
    monkeypatch.setattr(lifecycle, "ConnectionManager", Manager)
    monkeypatch.setattr(lifecycle, "SystemLogWriter", LogWriter)
    application = FastAPI()
    application.state.settings = Settings(
        _env_file=None,
        database_url="mysql+asyncmy://test:test@localhost/test",
        jwt_secret="test-lifespan-secret-with-at-least-thirty-two-characters",
        simulator_enabled=False,
    )
    context = lifecycle.lifespan(application)
    await context.__aenter__()
    exiting = asyncio.create_task(context.__aexit__(None, None, None))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        exiting.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(exiting, timeout=1)
        assert closed == [True]
        assert disposed == [True]
        assert application.state.batch_writer.task.done()
        assert application.state.system_log_writer.task.done()
    finally:
        release.set()
        await asyncio.gather(exiting, return_exceptions=True)
