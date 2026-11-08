"""ORM-only sample storage. The caller owns the transaction and commit."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.realtime import RealtimeSample
from app.realtime.events import SampleEvent


async def add_missing_samples(session: AsyncSession, events: Sequence[SampleEvent]) -> int:
    # Stable event IDs cover both repeated deliveries and an uncertain commit result.
    unique = {str(event.event_id): event for event in events}
    if not unique:
        return 0
    existing = set(
        await session.scalars(
            select(RealtimeSample.event_id).where(RealtimeSample.event_id.in_(unique))
        )
    )
    samples = [
        RealtimeSample(
            event_id=event_id,
            device_id=event.device_id,
            device_name=event.device_name,
            unit=event.unit,
            title=event.title,
            value=event.value,
            category=event.category,
            timestamp=event.timestamp,
            threshold=event.threshold,
            is_anomaly=event.is_anomaly,
        )
        for event_id, event in unique.items()
        if event_id not in existing
    ]
    session.add_all(samples)
    await session.flush()
    return len(samples)


async def list_samples(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    category: str | None = None,
    q: str | None = None,
    device_id: str | None = None,
    event_id: str | None = None,
    is_anomaly: bool | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> tuple[list[RealtimeSample], int]:
    conditions = []
    if device_id is not None:
        conditions.append(RealtimeSample.device_id == device_id)
    if category is not None:
        conditions.append(RealtimeSample.category == category)
    if q is not None:
        conditions.append(RealtimeSample.title.icontains(q, autoescape=True))
    if event_id is not None:
        conditions.append(RealtimeSample.event_id == event_id)
    if is_anomaly is not None:
        conditions.append(RealtimeSample.is_anomaly == is_anomaly)
    if start_time is not None:
        conditions.append(RealtimeSample.timestamp >= start_time)
    if end_time is not None:
        conditions.append(RealtimeSample.timestamp < end_time)
    total = await session.scalar(select(func.count(RealtimeSample.id)).where(*conditions))
    samples = await session.scalars(
        select(RealtimeSample)
        .where(*conditions)
        .order_by(RealtimeSample.timestamp.desc(), RealtimeSample.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(samples), total or 0
