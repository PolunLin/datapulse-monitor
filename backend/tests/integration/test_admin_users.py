"""Admin mutations preserve access, revoke tokens and commit their audit atomically."""

import asyncio
import json
import socket

import pytest
import pytest_asyncio
import uvicorn
from sqlalchemy import func, select
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.core.exceptions import UnauthorizedError
from app.models.system_log import SystemLog
from app.models.user import User
from app.schemas.admin_users import AdminUserUpdate
from app.services.admin_users import AdminUserService
from app.services.auth import AuthService
from tests.conftest import PASSWORD

pytestmark = pytest.mark.integration


async def login_headers(client, account):
    response = await client.post(
        "/api/v1/auth/login", data={"username": account.email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def audit_rows(app, actor_id, target_id):
    async with app.state.session_factory() as session:
        return list(
            await session.scalars(
                select(SystemLog)
                .where(
                    SystemLog.kind == "audit",
                    SystemLog.action == "user.updated",
                    SystemLog.actor_id == actor_id,
                    SystemLog.target_user_id == target_id,
                )
                .order_by(SystemLog.id)
            )
        )


async def test_admin_user_routes_require_current_admin(client, make_account):
    target = await make_account("admin-users-target@example.com")
    for headers in (
        {},
        target.headers,
        (await make_account("admin-users-viewer@example.com", "viewer")).headers,
    ):
        expected = 401 if not headers else 403
        assert (await client.get("/api/v1/admin/users", headers=headers)).status_code == expected
        assert (
            await client.get(f"/api/v1/admin/users/{target.id}", headers=headers)
        ).status_code == expected
        assert (
            await client.patch(
                f"/api/v1/admin/users/{target.id}", headers=headers, json={"role": "admin"}
            )
        ).status_code == expected


async def test_user_list_filters_pagination_and_safe_projection(client, make_account):
    admin = await make_account("admin-users-manager@example.com", "admin")
    first = await make_account("list-a_one@example.com")
    second = await make_account("list-b@example.com", "viewer")
    third = await make_account("list-c@example.com", "viewer")
    disabled = await client.patch(
        f"/api/v1/admin/users/{third.id}", headers=admin.headers, json={"is_active": False}
    )
    assert disabled.status_code == 200
    page_one = await client.get(
        "/api/v1/admin/users", headers=admin.headers, params={"q": "list-", "page_size": 2}
    )
    assert page_one.status_code == 200
    assert page_one.json()["total"] == 3
    assert [row["id"] for row in page_one.json()["items"]] == [first.id, second.id]
    page_two = await client.get(
        "/api/v1/admin/users",
        headers=admin.headers,
        params={"q": "list-", "page": 2, "page_size": 2},
    )
    assert [row["id"] for row in page_two.json()["items"]] == [third.id]
    filtered = await client.get(
        "/api/v1/admin/users",
        headers=admin.headers,
        params={"role": "viewer", "is_active": "false"},
    )
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["id"] == third.id
    escaped = await client.get("/api/v1/admin/users", headers=admin.headers, params={"q": "_"})
    assert escaped.json()["total"] == 1
    assert escaped.json()["items"][0]["id"] == first.id

    detail = await client.get(f"/api/v1/admin/users/{first.id}", headers=admin.headers)
    assert detail.status_code == 200
    expected_keys = {"id", "email", "role", "is_active", "created_at", "updated_at"}
    assert set(detail.json()) == expected_keys
    assert all(set(row) == expected_keys for row in page_one.json()["items"])
    missing = await client.get("/api/v1/admin/users/2147483647", headers=admin.headers)
    assert missing.status_code == 404
    assert (
        await client.patch(
            "/api/v1/admin/users/2147483647", headers=admin.headers, json={"role": "viewer"}
        )
    ).status_code == 404


async def test_role_change_revokes_all_target_tokens_and_has_durable_audit(
    client, app, make_account
):
    admin = await make_account("admin-users-audit-manager@example.com", "admin")
    target = await make_account("admin-users-audit-target@example.com")
    second_headers = await login_headers(client, target)
    response = await client.patch(
        f"/api/v1/admin/users/{target.id}",
        headers={**admin.headers, "X-Request-ID": "role-change-audit-test"},
        json={"role": "viewer"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "viewer"
    assert response.json()["updated_at"] >= response.json()["created_at"]
    for headers in (target.headers, second_headers):
        assert (await client.get("/api/v1/users/me", headers=headers)).status_code == 401
    fresh = await login_headers(client, target)
    assert (await client.get("/api/v1/users/me", headers=fresh)).json()["role"] == "viewer"
    assert (await client.get("/api/v1/users/me", headers=admin.headers)).status_code == 200

    entries = await audit_rows(app, admin.id, target.id)
    assert len(entries) == 1
    assert entries[0].request_id == "role-change-audit-test"
    assert entries[0].changes == {
        "before": {"role": "user", "is_active": True},
        "after": {"role": "viewer", "is_active": True},
    }
    serialized_changes = json.dumps(entries[0].changes)
    assert PASSWORD not in serialized_changes and target.email not in serialized_changes
    assert "token" not in serialized_changes


async def test_disable_and_enable_require_new_login_and_preserve_record_ownership(
    client, make_account, record_payload
):
    admin = await make_account("admin-users-status-manager@example.com", "admin")
    target = await make_account("admin-users-status-target@example.com")
    record = await client.post("/api/v1/records", headers=target.headers, json=record_payload)
    assert record.status_code == 201
    disabled = await client.patch(
        f"/api/v1/admin/users/{target.id}", headers=admin.headers, json={"is_active": False}
    )
    assert disabled.status_code == 200 and disabled.json()["is_active"] is False
    assert (await client.get("/api/v1/users/me", headers=target.headers)).status_code == 401
    login = await client.post(
        "/api/v1/auth/login", data={"username": target.email, "password": PASSWORD}
    )
    assert login.status_code == 401
    enabled = await client.patch(
        f"/api/v1/admin/users/{target.id}", headers=admin.headers, json={"is_active": True}
    )
    assert enabled.status_code == 200 and enabled.json()["is_active"] is True
    assert (await client.get("/api/v1/users/me", headers=target.headers)).status_code == 401
    fresh = await login_headers(client, target)
    owned = await client.get(f"/api/v1/records/{record.json()['id']}", headers=fresh)
    assert owned.status_code == 200 and owned.json()["created_by"] == target.id


async def test_promotion_requires_new_credentials_before_admin_access(client, make_account):
    admin = await make_account("admin-users-promote-manager@example.com", "admin")
    target = await make_account("admin-users-promote-target@example.com", "viewer")
    response = await client.patch(
        f"/api/v1/admin/users/{target.id}", headers=admin.headers, json={"role": "admin"}
    )
    assert response.status_code == 200
    assert (await client.get("/api/v1/admin/users", headers=target.headers)).status_code == 401
    fresh = await login_headers(client, target)
    assert (await client.get("/api/v1/admin/users", headers=fresh)).status_code == 200


async def test_admin_cannot_change_self_even_with_another_admin_present(client, app, make_account):
    admin = await make_account("admin-users-self@example.com", "admin")
    for payload in ({"role": "viewer"}, {"is_active": False}):
        response = await client.patch(
            f"/api/v1/admin/users/{admin.id}", headers=admin.headers, json=payload
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "admin_self_change"
    await make_account("admin-users-second-manager@example.com", "admin")
    response = await client.patch(
        f"/api/v1/admin/users/{admin.id}", headers=admin.headers, json={"role": "user"}
    )
    assert response.status_code == 409
    profile = await client.get("/api/v1/users/me", headers=admin.headers)
    assert profile.status_code == 200 and profile.json()["role"] == "admin"
    assert await audit_rows(app, admin.id, admin.id) == []


async def test_noop_is_allowed_without_revocation_timestamp_change_or_audit(
    client, app, make_account
):
    admin = await make_account("admin-users-noop-manager@example.com", "admin")
    target = await make_account("admin-users-noop-target@example.com")
    before = (await client.get("/api/v1/users/me", headers=target.headers)).json()
    for account, payload in (
        (target, {"role": "user", "is_active": True}),
        (admin, {"role": "admin", "is_active": True}),
    ):
        response = await client.patch(
            f"/api/v1/admin/users/{account.id}", headers=admin.headers, json=payload
        )
        assert response.status_code == 200
        assert (await client.get("/api/v1/users/me", headers=account.headers)).status_code == 200
        assert await audit_rows(app, admin.id, account.id) == []
    after = (await client.get("/api/v1/users/me", headers=target.headers)).json()
    assert before["updated_at"] == after["updated_at"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"role": None},
        {"is_active": None},
        {"role": "owner"},
        {"is_active": "false"},
        {"is_active": 1},
        {"email": "changed@example.com"},
        {"password": "do-not-log-this-secret"},
        {"token_version": 0},
    ],
)
async def test_admin_patch_rejects_ambiguous_or_credential_fields(client, make_account, payload):
    admin = await make_account("admin-users-validation@example.com", "admin")
    response = await client.patch(
        f"/api/v1/admin/users/{admin.id}", headers=admin.headers, json=payload
    )
    assert response.status_code == 422
    assert "do-not-log-this-secret" not in response.text


@pytest.mark.parametrize(
    "params", [{"page": 0}, {"page_size": 101}, {"role": "owner"}, {"q": " "}, {"q": "a" * 321}]
)
async def test_admin_user_filters_are_bounded(client, make_account, params):
    admin = await make_account("admin-users-filters@example.com", "admin")
    response = await client.get("/api/v1/admin/users", headers=admin.headers, params=params)
    assert response.status_code == 422


async def test_audit_failure_rolls_back_user_update(client, app, make_account, monkeypatch):
    from app.services import admin_users

    admin = await make_account("admin-users-rollback-manager@example.com", "admin")
    target = await make_account("admin-users-rollback-target@example.com")
    principal = await AuthService(app.state.session_factory, app.state.settings).authenticate(
        admin.token
    )
    original = admin_users.append_user_audit

    def fail_after_adding_audit(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Simulated audit failure")

    monkeypatch.setattr(admin_users, "append_user_audit", fail_after_adding_audit)
    with pytest.raises(RuntimeError, match="Simulated audit failure"):
        await AdminUserService(app.state.session_factory).update(
            principal, target.id, AdminUserUpdate(role="viewer")
        )
    profile = await client.get("/api/v1/users/me", headers=target.headers)
    assert profile.status_code == 200 and profile.json()["role"] == "user"
    assert await audit_rows(app, admin.id, target.id) == []


async def test_stale_actor_is_revalidated_after_locking(client, app, make_account):
    first = await make_account("admin-users-stale-first@example.com", "admin")
    second = await make_account("admin-users-stale-second@example.com", "admin")
    target = await make_account("admin-users-stale-target@example.com")
    stale = await AuthService(app.state.session_factory, app.state.settings).authenticate(
        first.token
    )
    response = await client.patch(
        f"/api/v1/admin/users/{first.id}", headers=second.headers, json={"role": "viewer"}
    )
    assert response.status_code == 200
    with pytest.raises(UnauthorizedError):
        await AdminUserService(app.state.session_factory).update(
            stale, target.id, AdminUserUpdate(role="admin")
        )
    profile = await client.get("/api/v1/users/me", headers=target.headers)
    assert profile.json()["role"] == "user"


async def test_concurrent_cross_demotion_preserves_an_active_administrator(
    client, app, make_account
):
    first = await make_account("admin-users-race-first@example.com", "admin")
    second = await make_account("admin-users-race-second@example.com", "admin")
    responses = await asyncio.gather(
        client.patch(
            f"/api/v1/admin/users/{second.id}", headers=first.headers, json={"role": "viewer"}
        ),
        client.patch(
            f"/api/v1/admin/users/{first.id}", headers=second.headers, json={"role": "viewer"}
        ),
    )
    assert sorted(response.status_code for response in responses) == [200, 401]
    async with app.state.session_factory() as session:
        remaining = await session.scalar(
            select(func.count(User.id)).where(User.is_active.is_(True), User.role == "admin")
        )
    assert remaining == 1


@pytest_asyncio.fixture
async def admin_websocket_url(app):
    app.state.ws_manager.auth_check_interval = 0.02
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"ws://127.0.0.1:{port}/ws/v1/samples"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        finally:
            listener.close()


async def test_role_change_closes_existing_websocket(admin_websocket_url, client, make_account):
    admin = await make_account("admin-users-ws-manager@example.com", "admin")
    target = await make_account("admin-users-ws-target@example.com")
    async with connect(
        admin_websocket_url, additional_headers=target.headers, proxy=None
    ) as websocket:
        ready = json.loads(await asyncio.wait_for(websocket.recv(), timeout=3))
        assert ready["type"] == "ready"
        response = await client.patch(
            f"/api/v1/admin/users/{target.id}", headers=admin.headers, json={"role": "viewer"}
        )
        assert response.status_code == 200
        with pytest.raises(ConnectionClosed) as exc:
            await asyncio.wait_for(websocket.recv(), timeout=3)
        assert exc.value.rcvd.code == 1008
