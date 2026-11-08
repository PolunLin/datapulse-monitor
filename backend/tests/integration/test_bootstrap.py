"""Migration reruns preserve legitimate management changes to the bootstrap account."""

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from app.cli import seed as bootstrap
from app.core.security import verify_password
from app.models.user import User
from tests.conftest import PASSWORD

pytestmark = pytest.mark.integration
BOOTSTRAP_PASSWORD = "Initial-Bootstrap-Password-Only-42!"


def configure_bootstrap(monkeypatch, settings, email):
    configured = settings.model_copy(
        update={
            "bootstrap_admin_email": email,
            "bootstrap_admin_password": SecretStr(BOOTSTRAP_PASSWORD),
        }
    )
    monkeypatch.setattr(bootstrap, "get_settings", lambda: configured)


async def account_snapshot(app, user_id):
    async with app.state.session_factory() as session:
        user = await session.get(User, user_id)
        assert user is not None
        return {column.name: getattr(user, column.name) for column in User.__table__.columns}


async def test_bootstrap_creates_admin_once_with_normalized_email(
    app, client, settings, monkeypatch, capsys
):
    configure_bootstrap(monkeypatch, settings, " Bootstrap-New@EXAMPLE.COM ")

    await bootstrap.seed()

    async with app.state.session_factory() as session:
        user = await session.scalar(select(User).where(User.email == "bootstrap-new@example.com"))
        assert user is not None
        assert user.role == "admin" and user.is_active and user.token_version == 0
        assert user.password_hash != BOOTSTRAP_PASSWORD
        assert verify_password(BOOTSTRAP_PASSWORD, user.password_hash)
        user_id = user.id
    before = await account_snapshot(app, user_id)

    await bootstrap.seed()

    assert await account_snapshot(app, user_id) == before
    async with app.state.session_factory() as session:
        assert await session.scalar(select(func.count(User.id))) == 1
    login = await client.post(
        "/api/v1/auth/login",
        data={"username": "bootstrap-new@example.com", "password": BOOTSTRAP_PASSWORD},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get("/api/v1/admin/users", headers=headers)).status_code == 200
    output = capsys.readouterr().out
    assert "Bootstrap admin created" in output and "left unchanged" in output
    assert BOOTSTRAP_PASSWORD not in output and before["password_hash"] not in output


@pytest.mark.parametrize("role", ["admin", "user", "viewer"])
@pytest.mark.parametrize("is_active", [True, False])
async def test_bootstrap_rerun_preserves_managed_role_status_password_and_token_version(
    app, client, settings, make_account, monkeypatch, capsys, role, is_active
):
    manager = await make_account("bootstrap-manager@example.com", "admin")
    original = await make_account("bootstrap-existing@example.com", "admin")
    configure_bootstrap(monkeypatch, settings, original.email)

    changed = await client.patch(
        f"/api/v1/admin/users/{original.id}",
        headers=manager.headers,
        json={"role": role, "is_active": is_active},
    )
    assert changed.status_code == 200
    before = await account_snapshot(app, original.id)
    was_changed = role != "admin" or not is_active
    assert before["token_version"] == int(was_changed)

    # This is the same entry point executed after Alembic by the Compose migrate service.
    # It uses a fresh Engine against the disposable MariaDB fixture, as a rerun would.
    await bootstrap.seed()
    await bootstrap.seed()

    assert await account_snapshot(app, original.id) == before
    async with app.state.session_factory() as session:
        assert await session.scalar(select(func.count(User.id))) == 2
    previous_token = await client.get("/api/v1/users/me", headers=original.headers)
    assert previous_token.status_code == (401 if was_changed else 200)
    login = await client.post(
        "/api/v1/auth/login", data={"username": original.email, "password": PASSWORD}
    )
    assert login.status_code == (200 if is_active else 401)
    if is_active:
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        assert (await client.get("/api/v1/users/me", headers=headers)).json()["role"] == role
        admin_access = await client.get("/api/v1/admin/users", headers=headers)
        assert admin_access.status_code == (200 if role == "admin" else 403)
    reset_attempt = await client.post(
        "/api/v1/auth/login",
        data={"username": original.email, "password": BOOTSTRAP_PASSWORD},
    )
    assert reset_attempt.status_code == 401
    assert (await client.get("/api/v1/admin/users", headers=manager.headers)).status_code == 200
    output = capsys.readouterr().out
    assert "left unchanged" in output
    assert BOOTSTRAP_PASSWORD not in output and PASSWORD not in output
