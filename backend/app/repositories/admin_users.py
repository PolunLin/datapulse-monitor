"""Bounded user queries and deterministic row locking for administrative changes."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.admin_users import AdminUserFilters


async def list_page(session: AsyncSession, filters: AdminUserFilters) -> tuple[list[User], int]:
    conditions = []
    if filters.q is not None:
        conditions.append(User.email.icontains(filters.q, autoescape=True))
    if filters.role is not None:
        conditions.append(User.role == filters.role.value)
    if filters.is_active is not None:
        conditions.append(User.is_active == filters.is_active)
    total = await session.scalar(select(func.count(User.id)).where(*conditions))
    result = await session.scalars(
        select(User)
        .where(*conditions)
        .order_by(User.id)
        .offset((filters.page - 1) * filters.page_size)
        .limit(filters.page_size)
    )
    return list(result), total or 0


async def lock_users(session: AsyncSession, actor_id: int, target_id: int) -> dict[int, User]:
    # Separate primary-key lookups guarantee lock order without relying on a query plan.
    # The actor remains an active Admin until commit; no operation can disable itself.
    locked = {}
    for user_id in sorted({actor_id, target_id}):
        user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is not None:
            locked[user_id] = user
    return locked
