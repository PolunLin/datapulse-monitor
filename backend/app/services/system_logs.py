from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionFactory
from app.models.system_log import SystemLog
from app.schemas.system import LogFilters, LogPage, LogRead
from app.services.auth import Principal
from app.services.authorization import require_admin


def append_user_audit(
    session: AsyncSession,
    *,
    actor_id: int,
    target_id: int,
    before: dict,
    after: dict,
    request_id: str | None,
) -> None:
    """Participate in the user mutation's transaction; a failed audit rolls it back."""
    changes = {
        "before": {field: before[field] for field in ("role", "is_active")},
        "after": {field: after[field] for field in ("role", "is_active")},
    }
    session.add(
        SystemLog(
            level="INFO",
            kind="audit",
            logger="app.admin.users",
            message="Administrator updated user access",
            request_id=request_id,
            actor_id=actor_id,
            target_user_id=target_id,
            action="user.updated",
            changes=changes,
        )
    )


class SystemLogService:
    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    async def list(self, principal: Principal, filters: LogFilters) -> LogPage:
        require_admin(principal)
        conditions = []
        for name in ("level", "kind", "request_id", "actor_id"):
            value = getattr(filters, name)
            if value is not None:
                conditions.append(getattr(SystemLog, name) == value)
        if filters.q is not None:
            conditions.append(SystemLog.message.icontains(filters.q, autoescape=True))
        if filters.start_time is not None:
            conditions.append(SystemLog.timestamp >= filters.start_time)
        if filters.end_time is not None:
            conditions.append(SystemLog.timestamp < filters.end_time)
        async with self.session_factory.begin() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            total = await session.scalar(select(func.count(SystemLog.id)).where(*conditions))
            rows = await session.scalars(
                select(SystemLog)
                .where(*conditions)
                .order_by(SystemLog.timestamp.desc(), SystemLog.id.desc())
                .offset((filters.page - 1) * filters.page_size)
                .limit(filters.page_size)
            )
            return LogPage(
                items=[LogRead.model_validate(row) for row in rows],
                total=total or 0,
                page=filters.page,
                page_size=filters.page_size,
            )
