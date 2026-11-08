from datetime import UTC, datetime, timedelta

import jwt
import pytest

from tests.conftest import PASSWORD

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_registration_normalizes_email_and_never_returns_credentials(client):
    response = await client.post(
        "/api/v1/auth/register", json={"email": "ALICE@Example.COM", "password": PASSWORD}
    )
    assert response.status_code == 201
    user = response.json()
    assert user["email"] == "alice@example.com"
    assert user["role"] == "user"
    assert "password" not in user
    assert "password_hash" not in user
    assert "token_version" not in user

    duplicate = await client.post(
        "/api/v1/auth/register", json={"email": "alice@example.com", "password": PASSWORD}
    )
    assert duplicate.status_code == 409


async def test_registration_cannot_choose_privileged_role(client):
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "self-admin@example.com", "password": PASSWORD, "role": "admin"},
    )
    assert response.status_code == 422


async def test_registration_rejects_invalid_credentials(client):
    for body in (
        {"email": "not-an-email", "password": PASSWORD},
        {"email": "weak@example.com", "password": "abc"},
        {"email": "long@example.com", "password": "a" * 129},
    ):
        response = await client.post("/api/v1/auth/register", json=body)
        assert response.status_code == 422, response.text
        assert body["password"] not in response.text


async def test_login_me_and_invalid_credentials(client, make_account):
    account = await make_account("login@example.com")
    me = await client.get("/api/v1/users/me", headers=account.headers)
    assert me.status_code == 200
    assert me.json()["id"] == account.id
    assert "password_hash" not in me.json()

    for username, password in (
        (account.email, "incorrect-password"),
        ("missing@example.com", PASSWORD),
    ):
        response = await client.post(
            "/api/v1/auth/login", data={"username": username, "password": password}
        )
        assert response.status_code == 401
        assert response.headers["www-authenticate"].lower() == "bearer"


async def test_missing_malformed_expired_and_invalid_signature_tokens(
    client, make_account, settings
):
    account = await make_account("tokens@example.com")
    payload = jwt.decode(account.token, options={"verify_signature": False})
    secret = settings.jwt_secret.get_secret_value()
    expired = jwt.encode(
        {**payload, "exp": datetime.now(UTC) - timedelta(minutes=1)}, secret, algorithm="HS256"
    )
    forged = jwt.encode(payload, "different-secret-with-at-least-32-characters", algorithm="HS256")
    no_expiry = jwt.encode(
        {key: value for key, value in payload.items() if key != "exp"}, secret, algorithm="HS256"
    )
    unsigned = jwt.encode(payload, "", algorithm="none")

    for token in (None, "not.a.jwt", expired, forged, no_expiry, unsigned):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = await client.get("/api/v1/users/me", headers=headers)
        assert response.status_code == 401, response.text


async def test_tokens_for_wrong_issuer_or_audience_are_rejected(client, make_account, settings):
    account = await make_account("claims@example.com")
    payload = jwt.decode(account.token, options={"verify_signature": False})
    for changed in ({"iss": "another-service"}, {"aud": "another-api"}):
        token = jwt.encode(
            {**payload, **changed}, settings.jwt_secret.get_secret_value(), algorithm="HS256"
        )
        response = await client.get(
            "/api/v1/users/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401


async def test_logout_revokes_all_existing_tokens_and_allows_new_login(client, make_account):
    account = await make_account("logout@example.com")
    second_login = await client.post(
        "/api/v1/auth/login", data={"username": account.email, "password": PASSWORD}
    )
    assert second_login.status_code == 200
    second_token = second_login.json()["access_token"]

    logout = await client.post("/api/v1/auth/logout", headers=account.headers)
    assert logout.status_code == 204
    assert logout.content == b""
    for token in (account.token, second_token):
        response = await client.get(
            "/api/v1/users/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401

    login = await client.post(
        "/api/v1/auth/login", data={"username": account.email, "password": PASSWORD}
    )
    assert login.status_code == 200
    assert login.json()["token_type"].lower() == "bearer"
    assert login.json()["expires_in"] > 0
    response = await client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"}
    )
    assert response.status_code == 200


async def test_disabled_user_loses_access_and_cannot_log_in(client, make_account, app):
    from app.models.user import User

    account = await make_account("disabled@example.com")
    async with app.state.session_factory() as session:
        async with session.begin():
            user = await session.get(User, account.id)
            user.is_active = False

    response = await client.get("/api/v1/users/me", headers=account.headers)
    assert response.status_code == 401
    login = await client.post(
        "/api/v1/auth/login", data={"username": account.email, "password": PASSWORD}
    )
    assert login.status_code == 401
