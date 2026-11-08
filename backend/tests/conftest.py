"""Integration fixtures: run only against a disposable, migrated MariaDB database."""

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.engine import make_url

PASSWORD = "Correct-Horse-Phase1-42!"
TEST_SECRET = "datapulse-integration-test-secret-only-123456789"


@dataclass(frozen=True)
class Account:
    id: int
    email: str
    token: str
    password: str = PASSWORD

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture
def settings():
    from app.core.config import Settings

    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.fail(
            "Set TEST_DATABASE_URL to a migrated MariaDB 11.7 test database.", pytrace=False
        )
    url = make_url(database_url)
    if url.drivername != "mysql+asyncmy" or not (url.database or "").endswith("_test"):
        pytest.fail(
            "Refusing database cleanup: use mysql+asyncmy and a database name ending in _test.",
            pytrace=False,
        )
    return Settings(
        _env_file=None, database_url=database_url, jwt_secret=TEST_SECRET, simulator_enabled=False
    )


@pytest_asyncio.fixture
async def app(settings) -> AsyncIterator[Any]:
    from app.main import create_app
    from app.models.realtime import RealtimeSample
    from app.models.record import Record
    from app.models.system_log import SystemLog
    from app.models.user import User

    application = create_app(settings)

    async def clean_database() -> None:
        async with application.state.session_factory() as session:
            async with session.begin():
                await session.execute(delete(RealtimeSample))
                await session.execute(delete(SystemLog))
                await session.execute(delete(Record))
                await session.execute(delete(User))

    async with application.router.lifespan_context(application):
        await clean_database()
        try:
            yield application
        finally:
            await clean_database()


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as session:
        yield session


@pytest.fixture
def make_account(client, app) -> Callable[..., Awaitable[Account]]:
    async def create(email: str, role: str = "user") -> Account:
        from app.models.user import User

        response = await client.post(
            "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
        )
        assert response.status_code == 201, response.text
        user = response.json()
        if role != "user":
            async with app.state.session_factory() as session:
                async with session.begin():
                    model = await session.get(User, user["id"])
                    assert model is not None
                    model.role = role
        login = await client.post(
            "/api/v1/auth/login", data={"username": email, "password": PASSWORD}
        )
        assert login.status_code == 200, login.text
        return Account(id=user["id"], email=user["email"], token=login.json()["access_token"])

    return create


@pytest.fixture
def record_payload() -> dict[str, Any]:
    return {
        "title": "cpu sample",
        "value": "23.125",
        "category": "device-001",
        "timestamp": "2026-11-08T09:00:00+08:00",
    }
