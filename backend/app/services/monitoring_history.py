from app.db.session import SessionFactory
from app.repositories import realtime
from app.schemas.monitoring import (
    SampleFeed,
    SampleFeedFilters,
    SampleFilters,
    SamplePage,
    SampleRead,
)
from app.services.auth import Principal
from app.services.authorization import require_admin, require_sample_reader


class MonitoringHistoryService:
    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    async def list(self, principal: Principal, filters: SampleFilters) -> SamplePage:
        require_admin(principal)
        return await self._list_samples(filters)

    async def list_monitoring(self, principal: Principal, filters: SampleFilters) -> SamplePage:
        require_sample_reader(principal)
        return await self._list_samples(filters)

    async def feed(self, principal: Principal, filters: SampleFeedFilters) -> SampleFeed:
        require_sample_reader(principal)
        async with self.session_factory.begin() as session:
            samples, next_cursor, has_more = await realtime.list_sample_feed(
                session,
                device_id=filters.device_id,
                after_id=filters.after_id,
                limit=filters.limit,
            )
            return SampleFeed(
                items=[SampleRead.model_validate(sample) for sample in samples],
                next_cursor=next_cursor,
                has_more=has_more,
            )

    async def _list_samples(self, filters: SampleFilters) -> SamplePage:
        async with self.session_factory.begin() as session:
            # Count and page are from one snapshot while new equipment samples arrive.
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            samples, total = await realtime.list_samples(
                session,
                page=filters.page,
                page_size=filters.page_size,
                category=filters.category,
                q=filters.q,
                device_id=filters.device_id,
                event_id=str(filters.event_id) if filters.event_id else None,
                is_anomaly=filters.is_anomaly,
                start_time=filters.start_time,
                end_time=filters.end_time,
            )
            return SamplePage(
                items=[SampleRead.model_validate(sample) for sample in samples],
                total=total,
                page=filters.page,
                page_size=filters.page_size,
            )
