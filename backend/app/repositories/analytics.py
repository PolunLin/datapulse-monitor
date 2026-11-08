"""SQLAlchemy ORM aggregates; only already-committed database rows are queried."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Numeric, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.exceptions import ApplicationError
from app.models.realtime import RealtimeSample
from app.models.record import Record
from app.schemas.analytics import (
    AggregateStatistics,
    AnalyticsFilters,
    CategoryStatistics,
    DataSource,
    TrendStatistics,
)

MAX_AGGREGATE_GROUPS = 1000


class AnalyticsLimitError(ApplicationError):
    status_code = 422
    code = "analytics_limit_exceeded"


def model_for_source(source: DataSource) -> type[Record] | type[RealtimeSample]:
    return RealtimeSample if source == "realtime" else Record


def build_conditions(filters: AnalyticsFilters) -> list[ColumnElement[bool]]:
    model = model_for_source(filters.source)
    conditions = []
    if filters.category is not None:
        conditions.append(model.category == filters.category)
    if filters.q is not None:
        conditions.append(model.title.icontains(filters.q, autoescape=True))
    if filters.created_by is not None:
        # AnalyticsFilters rejects created_by for realtime before repository access.
        conditions.append(Record.created_by == filters.created_by)
    if filters.device_id is not None:
        conditions.append(RealtimeSample.device_id == filters.device_id)
    if filters.event_id is not None:
        conditions.append(RealtimeSample.event_id == str(filters.event_id))
    if filters.is_anomaly is not None:
        conditions.append(RealtimeSample.is_anomaly == filters.is_anomaly)
    if filters.start_time is not None:
        conditions.append(model.timestamp >= filters.start_time)
    if filters.end_time is not None:
        conditions.append(model.timestamp < filters.end_time)
    return conditions


def statistic_columns(model: type[Record] | type[RealtimeSample]):
    # MariaDB returns DECIMAL aggregates. Explicit result types avoid float conversion;
    # AVG of DECIMAL(..., 6) has ten fractional digits with MariaDB's default settings.
    return (
        func.count(model.id).label("count"),
        func.sum(model.value, type_=Numeric(38, 6)).label("total"),
        func.avg(model.value, type_=Numeric(38, 10)).label("average"),
        func.min(model.value).label("minimum"),
        func.max(model.value).label("maximum"),
    )


async def summarize(session: AsyncSession, filters: AnalyticsFilters) -> AggregateStatistics:
    result = await session.execute(
        select(*statistic_columns(model_for_source(filters.source))).where(
            *build_conditions(filters)
        )
    )
    values = dict(result.mappings().one())
    if values["total"] is None:
        values["total"] = Decimal(0)
    return AggregateStatistics.model_validate(values)


async def by_category(session: AsyncSession, filters: AnalyticsFilters) -> list[CategoryStatistics]:
    model = model_for_source(filters.source)
    # Manual records classify by device ID in category, while system measurements
    # retain their metric category (temperature) and carry a separate device ID.
    # Compare devices for either source without conflating those storage fields.
    grouping = model.device_id if filters.source == "realtime" else model.category
    result = await session.execute(
        select(grouping.label("category"), *statistic_columns(model))
        .where(*build_conditions(filters))
        .group_by(grouping)
        .order_by(grouping)
        .limit(MAX_AGGREGATE_GROUPS + 1)
    )
    rows = result.mappings().all()
    if len(rows) > MAX_AGGREGATE_GROUPS:
        raise AnalyticsLimitError(
            "More than 1000 categories match; narrow the category, title or time filters"
        )
    return [CategoryStatistics.model_validate(row) for row in rows]


async def by_time(session: AsyncSession, filters: AnalyticsFilters) -> list[TrendStatistics]:
    model = model_for_source(filters.source)
    # Storage/response timestamps remain UTC. Taiwan's UTC+8 hour boundaries match
    # UTC hours; local midnight is 16:00 UTC on the preceding day. Subtracting 16h
    # before date grouping also avoids overflowing DATETIME at the upper year bound.
    # ORM functions use an explicit offset, independent of DB/server timezone tables.
    bucket = (
        func.date_format(model.timestamp, "%Y-%m-%dT%H:00:00")
        if filters.interval == "hour"
        else func.date_format(func.subtime(model.timestamp, "16:00:00"), "%Y-%m-%dT16:00:00")
    ).label("timestamp")
    result = await session.execute(
        select(bucket, *statistic_columns(model))
        .where(*build_conditions(filters))
        .group_by(bucket)
        .order_by(bucket)
        .limit(MAX_AGGREGATE_GROUPS + 1)
    )
    rows = result.mappings().all()
    if len(rows) > MAX_AGGREGATE_GROUPS:
        raise AnalyticsLimitError(
            "More than 1000 time buckets match; narrow the time range or select day intervals"
        )
    return [
        TrendStatistics.model_validate(
            {**row, "timestamp": datetime.fromisoformat(row["timestamp"]).replace(tzinfo=UTC)}
        )
        for row in rows
    ]
