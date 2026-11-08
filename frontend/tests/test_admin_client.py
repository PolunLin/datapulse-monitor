"""Admin calls remain authenticated, scoped and safe on rejected mutations."""

import json

import httpx
import pytest

from clients.api import APIClient, APIError


@pytest.mark.parametrize(
    "method,args,expected_method,path",
    [
        ("admin_users", (), "GET", "/api/v1/admin/users"),
        ("admin_user", (7,), "GET", "/api/v1/admin/users/7"),
        (
            "update_admin_user",
            (7, {"role": "viewer", "is_active": False}),
            "PATCH",
            "/api/v1/admin/users/7",
        ),
        ("admin_logs", (), "GET", "/api/v1/admin/logs"),
        ("admin_logging_status", (), "GET", "/api/v1/admin/logging-status"),
        ("admin_database", (), "GET", "/api/v1/admin/database"),
    ],
)
def test_admin_endpoints_use_authorization_and_exact_resource(method, args, expected_method, path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = APIClient("http://api.test", "private-token", transport=httpx.MockTransport(handler))
    assert getattr(client, method)(*args) == {"ok": True}
    request = requests[0]
    assert request.method == expected_method and request.url.path == path
    assert request.headers["Authorization"] == "Bearer private-token"
    if method == "update_admin_user":
        assert json.loads(request.content) == {"role": "viewer", "is_active": False}


def test_admin_filters_preserve_false_and_remove_none():
    requests = []
    client = APIClient(
        "http://api.test",
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200, json={})
        ),
    )
    client.admin_users(q="a%_", is_active=False, role=None, page=2, page_size=20)
    assert dict(requests[0].url.params) == {
        "q": "a%_",
        "is_active": "false",
        "page": "2",
        "page_size": "20",
    }
    client.admin_logs(kind="audit", actor_id=8, start_time="2026-11-08T00:00:00Z", q=None)
    assert dict(requests[1].url.params) == {
        "kind": "audit",
        "actor_id": "8",
        "start_time": "2026-11-08T00:00:00Z",
    }
    client.monitoring_history(is_anomaly=False, event_id=None)
    assert dict(requests[2].url.params) == {"is_anomaly": "false"}


@pytest.mark.parametrize("status", [401, 403, 409, 503])
def test_admin_mutation_errors_never_show_backend_bodies(status):
    client = APIClient(
        "http://api.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, json={"error": {"message": "password=secret token=private"}}
            )
        ),
    )
    with pytest.raises(APIError) as captured:
        client.update_admin_user(7, {"is_active": False})
    assert captured.value.status_code == status
    assert "secret" not in str(captured.value) and "private" not in str(captured.value)
    if status == 409:
        assert "自己的角色" in str(captured.value)
        assert "email" not in str(captured.value)
