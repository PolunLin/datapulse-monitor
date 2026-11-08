from sqlalchemy import select

from app.db.session import SessionFactory
from app.models.realtime import RealtimeSample
from app.models.system_log import SystemLog
from app.models.user import User


async def check_database(session_factory: SessionFactory) -> None:
    async with session_factory() as session:
        # This verifies both connectivity and the application schema using ORM.
        await session.scalar(select(User.id).limit(1))
        await session.scalar(select(RealtimeSample).limit(1))
        await session.scalar(select(SystemLog.id).limit(1))
