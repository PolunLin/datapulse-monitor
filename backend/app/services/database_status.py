import asyncio
import time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.db.base import utc_now
from app.db.session import SessionFactory
from app.models.realtime import RealtimeSample
from app.models.record import Record
from app.models.system_log import SystemLog
from app.models.user import User
from app.schemas.system import DatabaseCounts, DatabaseStatus, PoolStatus
from app.services.auth import Principal
from app.services.authorization import require_admin


class DatabaseStatusService:
    def __init__(
        self, session_factory: SessionFactory, engine: AsyncEngine, settings: Settings
    ) -> None:
        self.session_factory = session_factory
        self.engine = engine
        self.settings = settings

    async def _probe(self) -> tuple[DatabaseCounts, object]:
        # One ORM query provides consistent counters without SHOW or raw SQL.
        statement = select(
            select(func.count(User.id)).scalar_subquery().label("users"),
            select(func.count(User.id))
            .where(User.is_active.is_(True))
            .scalar_subquery()
            .label("active_users"),
            select(func.count(Record.id)).scalar_subquery().label("records"),
            select(func.count(RealtimeSample.id)).scalar_subquery().label("realtime_samples"),
            select(func.count(SystemLog.id)).scalar_subquery().label("system_logs"),
            select(func.max(RealtimeSample.timestamp)).scalar_subquery().label("latest_sample_at"),
        )
        async with self.session_factory() as session:
            row = dict((await session.execute(statement)).mappings().one())
            latest = row.pop("latest_sample_at")
            return DatabaseCounts.model_validate(row), latest

    async def get(self, principal: Principal) -> DatabaseStatus:
        require_admin(principal)
        started = time.perf_counter()
        error = None
        counts = latest = None
        try:
            counts, latest = await asyncio.wait_for(self._probe(), timeout=3.0)
        except Exception:
            error = "Database probe unavailable"
        pool = self.engine.pool
        version = self.engine.dialect.server_version_info
        return DatabaseStatus(
            status="healthy" if error is None else "unavailable",
            checked_at=utc_now(),
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            server_version=".".join(str(part) for part in version) if version else None,
            counts=counts,
            latest_sample_at=latest,
            pool=PoolStatus(
                size=pool.size(),
                checked_in=pool.checkedin(),
                checked_out=pool.checkedout(),
                overflow=max(0, pool.overflow()),
                max_overflow=self.settings.db_max_overflow,
                timeout_seconds=float(pool.timeout()),
            ),
            error=error,
        )
