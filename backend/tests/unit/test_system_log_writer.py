import asyncio
import json
import logging
import time
from types import SimpleNamespace

import pytest

from app.core.system_log_writer import SystemLogWriter, event_from_record
from app.services.database_status import DatabaseStatusService


def event(message="Application resources initialized"):
    return event_from_record(
        logging.LogRecord("app.lifecycle", logging.INFO, "", 0, message, (), None)
    )


def test_persisted_logging_excludes_unknown_messages_arguments_and_exception_text():
    secret = "password=secret token=private SQL=SELECT-private"
    record = logging.LogRecord(
        "app.realtime.writer",
        logging.ERROR,
        "",
        0,
        "Batch persistence retry after %s",
        (secret,),
        (ValueError, ValueError(secret), None),
    )
    payload = event_from_record(record)
    assert payload["message"] == "Batch persistence retry"
    assert payload["error_type"] == "ValueError"
    assert secret not in json.dumps(payload, default=str)
    unknown = logging.LogRecord("app." + secret, logging.WARNING, "", 0, secret, (), None)
    payload = event_from_record(unknown)
    assert payload["message"] == "Application event"
    assert payload["logger"] == "app.other"
    assert secret not in json.dumps(payload, default=str)


def test_request_fields_are_bounded_allowlisted_and_never_raw_urls():
    record = logging.LogRecord(
        "app.requests", logging.WARNING, "", 0, "HTTP request completed", (), None
    )
    record.path = "/password=private?token=secret"
    record.method = "private-method"
    record.request_id = "secret.with.dots"
    record.actor_id = True
    record.duration_ms = float("nan")
    record.status_code = 9999
    payload = event_from_record(record)
    assert payload["path"] == "[unmatched]"
    assert payload["method"] == "OTHER"
    assert payload["request_id"] is payload["actor_id"] is payload["duration_ms"] is None
    assert payload["status_code"] is None
    record.path = "/api/v1/admin/users/{user_id}"
    record.method, record.request_id, record.actor_id = "PATCH", "request_42", 42
    record.status_code, record.duration_ms = 200, 1.234
    payload = event_from_record(record)
    assert payload["path"] == record.path
    assert payload["kind"] == "request"
    assert payload["actor_id"] == 42
    assert payload["request_id"] == "request_42"


async def test_log_queue_bounds_pending_and_removes_handler_after_shutdown(monkeypatch):
    writer = SystemLogWriter(object(), queue_capacity=3, batch_size=2, flush_interval=20)
    batches = []

    async def persist(batch):
        batches.append(list(batch))

    monkeypatch.setattr(writer, "_persist", persist)
    for _ in range(5):
        writer.enqueue(event())
    assert writer.pending_count == 3 and writer.dropped_count == 2
    writer.start()
    assert writer.handler in logging.getLogger("app").handlers
    assert await writer.stop(timeout=1)
    assert writer.handler not in logging.getLogger("app").handlers
    assert writer.pending_count == 0 and writer.persisted_count == 3
    assert sum(len(batch) for batch in batches) == 3
    assert writer.task.done()
    assert not writer.enqueue(event())
    assert writer.dropped_count == 3


async def test_inflight_batch_counts_toward_capacity_and_stop_is_bounded(monkeypatch):
    entered = asyncio.Event()
    writer = SystemLogWriter(object(), queue_capacity=2, batch_size=1, flush_interval=0.01)

    async def persist(_batch):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(writer, "_persist", persist)
    writer.enqueue(event())
    writer.start()
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert writer.enqueue(event())
    assert not writer.enqueue(event())
    assert writer.pending_count == 2
    started = time.monotonic()
    assert not await writer.stop(timeout=0.03)
    assert time.monotonic() - started < 0.5
    assert writer.pending_count == 0 and writer.dropped_count == 3
    assert writer.task.done()


async def test_log_database_failure_retries_without_recursion_and_reports_drops(monkeypatch):
    entered = asyncio.Event()
    writer = SystemLogWriter(object(), flush_interval=0.01)

    async def fail(_batch):
        entered.set()
        raise RuntimeError("password=never-expose")

    monkeypatch.setattr(writer, "_persist", fail)
    writer.enqueue(event())
    writer.start()
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert not await writer.stop(timeout=1)
    status = writer.snapshot()
    assert status.failure_count >= 1 and status.last_error == "RuntimeError"
    assert status.dropped_count == 1 and status.pending_count == 0
    assert "never-expose" not in status.model_dump_json()


async def test_subthreshold_log_batch_flushes_on_timer(monkeypatch):
    persisted = asyncio.Event()
    writer = SystemLogWriter(object(), flush_interval=0.01)

    async def persist(_batch):
        persisted.set()

    monkeypatch.setattr(writer, "_persist", persist)
    writer.start()
    writer.enqueue(event())
    await asyncio.wait_for(persisted.wait(), timeout=1)
    assert await writer.stop()
    assert writer.persisted_count == 1


async def test_empty_runtime_queue_still_runs_periodic_audit_retention(monkeypatch):
    checked = asyncio.Event()
    writer = SystemLogWriter(object(), flush_interval=0.01)

    async def persist(batch):
        assert batch == []
        checked.set()

    monkeypatch.setattr(writer, "_persist", persist)
    writer.start()
    await asyncio.wait_for(checked.wait(), timeout=1)
    assert await writer.stop()
    assert writer.persisted_count == 0


async def test_database_probe_failure_is_sanitized_and_negative_pool_overflow_clamped(monkeypatch):
    pool = SimpleNamespace(
        size=lambda: 5,
        checkedin=lambda: 1,
        checkedout=lambda: 0,
        overflow=lambda: -4,
        timeout=lambda: 30,
    )
    engine = SimpleNamespace(pool=pool, dialect=SimpleNamespace(server_version_info=(11, 7, 2)))
    service = DatabaseStatusService(object(), engine, SimpleNamespace(db_max_overflow=5))

    async def fail():
        raise RuntimeError("mysql+asyncmy://admin:private@server/db")

    monkeypatch.setattr(service, "_probe", fail)
    result = await service.get(SimpleNamespace(role="admin", is_active=True))
    assert result.status == "unavailable"
    assert result.error == "Database probe unavailable"
    assert result.counts is None and result.latest_sample_at is None
    assert result.pool.overflow == 0
    assert result.server_version == "11.7.2"
    assert "private" not in result.model_dump_json()


async def test_database_probe_does_not_start_for_non_admin(monkeypatch):
    from app.core.exceptions import ForbiddenError

    service = DatabaseStatusService(object(), object(), object())
    with pytest.raises(ForbiddenError):
        await service.get(SimpleNamespace(role="viewer", is_active=True))
