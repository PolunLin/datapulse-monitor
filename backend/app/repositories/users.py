from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    return await session.scalar(select(User).where(User.email == email))


async def get_by_id(
    session: AsyncSession, user_id: int, *, for_update: bool = False
) -> User | None:
    statement = select(User).where(User.id == user_id)
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def add(session: AsyncSession, user: User) -> User:
    session.add(user)
    await session.flush()
    return user
