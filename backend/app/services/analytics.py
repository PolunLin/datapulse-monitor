from app.db.session import SessionFactory
from app.repositories import analytics
from app.schemas.analytics import AnalyticsFilters, AnalyticsResult
from app.services.auth import Principal
from app.services.authorization import require_sample_reader


class AnalyticsService:
    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    async def analyze(self, principal: Principal, filters: AnalyticsFilters) -> AnalyticsResult:
        if filters.source == "realtime":
            require_sample_reader(principal)
        async with self.session_factory.begin() as session:
            # The first read establishes one consistent snapshot for all three queries,
            # even while the simulator or other users commit new measurements.
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            summary = await analytics.summarize(session, filters)
            categories = await analytics.by_category(session, filters)
            trend = await analytics.by_time(session, filters)
        return AnalyticsResult(
            source=filters.source,
            interval=filters.interval,
            summary=summary,
            categories=categories,
            trend=trend,
        )
