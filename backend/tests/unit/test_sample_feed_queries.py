"""The frequently polled feed stays a single bounded query without COUNT/OFFSET."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import mysql

from app.repositories.realtime import list_sample_feed
from app.schemas.device import DeviceCatalog


@pytest.mark.parametrize("after_id", [None, 50])
async def test_feed_executes_one_bounded_query_without_history_count_or_offset(after_id):
    session = SimpleNamespace(scalars=AsyncMock(return_value=[]))
    rows, cursor, has_more = await list_sample_feed(
        session, device_id="device-001", after_id=after_id, limit=30
    )
    assert (rows, cursor, has_more) == ([], after_id or 0, False)
    session.scalars.assert_awaited_once()
    statement = session.scalars.call_args.args[0]
    compiled = statement.compile(dialect=mysql.dialect())
    sql = str(compiled).upper()
    assert "COUNT(" not in sql and "OFFSET" not in sql
    assert "REALTIME_SAMPLES.DEVICE_ID =" in sql
    assert compiled.params["param_1"] == (30 if after_id is None else 31)
    if after_id is not None:
        assert "REALTIME_SAMPLES.ID >" in sql


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_catalog_rejects_nonpositive_or_nonfinite_sampling_interval(interval):
    with pytest.raises(ValidationError):
        DeviceCatalog(items=[], sample_interval_seconds=interval)
