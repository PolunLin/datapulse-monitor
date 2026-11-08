import asyncio

from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import create_database
from app.models.user import Role, User
from app.repositories import users


async def seed() -> None:
    settings = get_settings()
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        print("Bootstrap admin is not configured; seed skipped.")
        return

    engine, session_factory = create_database(settings)
    email = str(settings.bootstrap_admin_email).strip().casefold()
    try:
        async with session_factory() as session:
            existing = await users.get_by_email(session, email)
            if existing is not None:
                # Bootstrap creates an initial account, not a desired role/state.
                # Later administrator decisions must survive migration reruns.
                print("Bootstrap account already exists; account settings were left unchanged.")
                return

        hashed = await run_in_threadpool(
            hash_password, settings.bootstrap_admin_password.get_secret_value()
        )
        async with session_factory.begin() as session:
            await users.add(
                session,
                User(email=email, password_hash=hashed, role=Role.ADMIN.value),
            )
        print("Bootstrap admin created.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
