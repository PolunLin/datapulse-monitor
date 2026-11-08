"""Bounded buffering with timed/size flushes and retained retry batches."""

import asyncio
import logging
from collections import deque

from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from app.db.session import SessionFactory
from app.realtime.events import SampleEvent
from app.repositories.realtime import add_missing_samples

logger = logging.getLogger("app.realtime.writer")


class BatchWriter:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        batch_size: int = 100,
        flush_interval: float = 5.0,
        queue_capacity: int = 1000,
        retry_initial: float = 0.1,
        retry_max: float = 5.0,
    ) -> None:
        if batch_size < 1 or queue_capacity < 1 or flush_interval <= 0:
            raise ValueError("Batch size, interval and queue capacity must be positive")
        if retry_initial <= 0 or retry_max < retry_initial:
            raise ValueError("Retry delays must satisfy 0 < initial <= max")
        self.session_factory = session_factory
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.queue_capacity = queue_capacity
        self.retry_initial = retry_initial
        self.retry_max = retry_max
        self.task: asyncio.Task[None] | None = None
        self.persisted_count = 0
        self.failure_count = 0
        self.last_error: str | None = None
        self._queue: deque[SampleEvent] = deque()
        self._batch: list[SampleEvent] = []
        self._condition = asyncio.Condition()
        self._accepting = False
        self._failed = False

    @property
    def pending_count(self) -> int:
        return len(self._queue) + len(self._batch)

    @property
    def healthy(self) -> bool:
        return bool(
            self.task is not None
            and not self.task.done()
            and not self._failed
            and self.last_error is None
        )

    def start(self) -> None:
        if self.task is not None:
            raise RuntimeError("Batch writer has already been started")
        self._accepting = True
        self.task = asyncio.create_task(self._run(), name="realtime-batch-writer")
        self.task.add_done_callback(self._observe_completion)

    def _observe_completion(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()  # Retrieve failures even when no caller is awaiting shutdown yet.

    async def enqueue(self, event: SampleEvent) -> None:
        async with self._condition:
            while len(self._queue) >= self.queue_capacity and self._accepting and not self._failed:
                await self._condition.wait()
            if not self._accepting or self._failed or self.task is None or self.task.done():
                raise RuntimeError("Batch writer is not accepting events")
            self._queue.append(event)
            self._condition.notify_all()

    async def stop(self, timeout: float = 10.0) -> bool:
        """Drain within timeout, then allow at most min(timeout, 1s) for cancellation cleanup.

        A driver that ignores cancellation may still be running when this returns
        False. Pending events remain accounted for and the writer stays unhealthy.
        """
        if timeout <= 0:
            raise ValueError("Shutdown timeout must be positive")
        async with self._condition:
            self._accepting = False
            self._condition.notify_all()
        if self.task is None:
            return self.pending_count == 0
        if self.task.cancelled():
            return False
        try:
            await asyncio.wait_for(asyncio.shield(self.task), timeout=timeout)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if self.task.cancelled() and current is not None and not current.cancelling():
                return False
            raise
        except TimeoutError:
            self._failed = True
            self.last_error = "shutdown_timeout"
            self.task.cancel()
            completed, _ = await asyncio.wait({self.task}, timeout=min(timeout, 1.0))
            if not completed:
                logger.error("Batch writer cancellation cleanup exceeded its deadline")
                self.task.cancel()
            logger.error("Batch shutdown timed out with %d pending events", self.pending_count)
            return False
        except Exception:
            return False
        return self.pending_count == 0 and not self._failed

    async def _collect_batch(self) -> bool:
        async with self._condition:
            while not self._queue and self._accepting:
                await self._condition.wait()
            if not self._queue:
                return False
            deadline = asyncio.get_running_loop().time() + self.flush_interval
            while True:
                while self._queue and len(self._batch) < self.batch_size:
                    self._batch.append(self._queue.popleft())
                self._condition.notify_all()
                remaining = deadline - asyncio.get_running_loop().time()
                if len(self._batch) >= self.batch_size or not self._accepting or remaining <= 0:
                    return True
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except TimeoutError:
                    return True

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        return isinstance(exc, (OperationalError, SQLAlchemyTimeoutError)) or (
            isinstance(exc, DBAPIError) and exc.connection_invalidated
        )

    async def _persist_batch(self) -> None:
        delay = self.retry_initial
        while True:
            try:
                async with self.session_factory.begin() as session:
                    inserted = await add_missing_samples(session, self._batch)
            except Exception as exc:
                self.failure_count += 1
                self.last_error = type(exc).__name__
                if not self._retryable(exc):
                    raise
                logger.warning("Batch persistence retry after %s", self.last_error)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.retry_max)
                continue
            # Only discard accepted events once the transaction was committed, or a
            # retry confirmed that the same event IDs are already stored.
            self.persisted_count += inserted
            self._batch.clear()
            self.last_error = None
            return

    async def _run(self) -> None:
        try:
            while await self._collect_batch():
                await self._persist_batch()
        except asyncio.CancelledError:
            self._failed = True
            self.last_error = self.last_error or "writer_cancelled"
            raise
        except Exception as exc:
            self._failed = True
            self.last_error = type(exc).__name__
            logger.error("Batch writer stopped after %s", self.last_error)
            raise
        finally:
            async with self._condition:
                self._accepting = False
                self._condition.notify_all()
