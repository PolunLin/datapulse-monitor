import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.system_log_writer import SystemLogWriter
from app.db.session import create_database
from app.realtime.batch_writer import BatchWriter
from app.realtime.dispatcher import EventDispatcher
from app.realtime.generator import Simulator
from app.services.auth import AuthService
from app.ws.manager import ConnectionManager

logger = logging.getLogger("app.lifecycle")


def observe_worker(task: asyncio.Task) -> None:
    """Retrieve worker exceptions immediately; readiness also checks task state."""
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("Background task failed: %s", task.get_name(), exc_info=error)


async def close_resources(app: FastAPI, engine, writer: BatchWriter, manager: ConnectionManager):
    """Stop producers, drain accepted work, then release transports and database resources."""
    try:
        try:
            if app.state.simulator is not None:
                await asyncio.wait_for(app.state.simulator.stop(), timeout=2.0)
        except Exception:
            logger.error("Simulator shutdown failed", exc_info=True)
        finally:
            try:
                drained = await writer.stop(
                    timeout=app.state.settings.realtime_shutdown_timeout_seconds
                )
                if not drained:
                    logger.error(
                        "Shutdown left %s unconfirmed events in memory", writer.pending_count
                    )
            except Exception:
                logger.error("Batch writer shutdown failed", exc_info=True)
    finally:
        try:
            await asyncio.wait_for(manager.close_all(), timeout=5.0)
        except Exception:
            logger.error("WebSocket shutdown failed", exc_info=True)
        finally:
            try:
                log_writer = getattr(app.state, "system_log_writer", None)
                if log_writer is not None:
                    await log_writer.stop(timeout=2.0)
            except Exception:
                logger.error("System log writer shutdown failed", exc_info=True)
            finally:
                try:
                    await asyncio.wait_for(engine.dispose(), timeout=5.0)
                except Exception:
                    logger.error("Database resource cleanup failed", exc_info=True)
    logger.info("Application resources released")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = app.state.settings
    engine, session_factory = create_database(settings)
    app.state.engine = engine
    app.state.session_factory = session_factory
    log_writer = SystemLogWriter(session_factory)
    app.state.system_log_writer = log_writer
    writer = BatchWriter(
        session_factory,
        batch_size=settings.realtime_batch_size,
        flush_interval=settings.realtime_flush_interval_seconds,
        queue_capacity=settings.realtime_queue_capacity,
        retry_initial=settings.realtime_retry_initial_seconds,
        retry_max=settings.realtime_retry_max_seconds,
    )
    manager = ConnectionManager(
        AuthService(session_factory, settings),
        enabled=settings.ws_enabled,
        queue_capacity=settings.ws_queue_capacity,
        send_timeout=settings.ws_send_timeout_seconds,
        auth_check_interval=settings.ws_auth_check_interval_seconds,
        max_connections=settings.ws_max_connections,
    )
    dispatcher = EventDispatcher(writer, manager.publish)
    app.state.batch_writer = writer
    app.state.ws_manager = manager
    app.state.event_dispatcher = dispatcher
    app.state.simulator = None
    try:
        log_writer.start()
        writer.start()
        writer.task.add_done_callback(observe_worker)
        if settings.simulator_enabled:
            simulator = Simulator(
                dispatcher,
                interval=settings.simulator_interval_seconds,
                category=settings.simulator_category,
                threshold=settings.simulator_threshold,
            )
            app.state.simulator = simulator
            simulator.start()
            simulator.task.add_done_callback(observe_worker)
        logger.info("Application resources initialized")
        yield
    finally:
        cleanup = asyncio.create_task(
            close_resources(app, engine, writer, manager), name="application-resource-cleanup"
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # A cancelled lifespan caller must not interrupt a transaction drain or
            # skip engine disposal. Preserve cancellation after bounded cleanup.
            await asyncio.shield(cleanup)
            raise
