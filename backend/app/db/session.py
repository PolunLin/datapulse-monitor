from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings

SessionFactory = async_sessionmaker[AsyncSession]


def create_database(settings: Settings) -> tuple[AsyncEngine, SessionFactory]:
    engine = create_async_engine(
        settings.sqlalchemy_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
        hide_parameters=True,
        echo=False,
        connect_args={"connect_timeout": 5},
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)
