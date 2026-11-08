import json
from urllib.parse import parse_qs

import httpx
import pytest

from clients.api import APIClient, APIError


def test_login_uses_oauth_form_and_separate_sessions_keep_tokens_private():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"access_token": "token-one", "expires_in": 1800})
        return httpx.Response(200, json={"id": 1, "role": "user"})

    transport = httpx.MockTransport(handle)
    anon = APIClient("http://backend:8000", transport=transport)
    token = anon.login(" user@example.com ", "password&=literal")["access_token"]
    first = APIClient("http://backend:8000", token, transport=transport)
    second = APIClient("http://backend:8000", "token-two", transport=transport)
    first.me()
    second.me()
    assert "Authorization" not in requests[0].headers
    assert parse_qs(requests[0].content.decode()) == {
        "username": ["user@example.com"],
        "password": ["password&=literal"],
    }
    assert requests[1].headers["Authorization"] == "Bearer token-one"
    assert requests[2].headers["Authorization"] == "Bearer token-two"
    assert token not in repr(first)


def test_crud_routes_filters_and_decimal_payload_are_preserved():
    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "DELETE" or request.url.path.endswith("/logout"):
            return httpx.Response(204)
        return httpx.Response(200, json={"id": 7})

    client = APIClient("http://backend:8000", "secret", transport=httpx.MockTransport(handle))
    client.register(" user@example.com ", "private-password")
    client.list_records(category="溫度 A & B", page=2, page_size=20, q=None)
    client.get_record(7)
    record = {"title": "A", "value": "900719925.123456", "category": "溫度"}
    client.create_record(record)
    client.update_record(7, {"value": "0.000001"})
    assert client.delete_record(7) is None
    client.monitoring_status()
    client.monitoring_history(event_id="id", start_time=None)
    assert client.logout() is None
    assert [(r.method, r.url.path) for r in requests] == [
        ("POST", "/api/v1/auth/register"),
        ("GET", "/api/v1/records"),
        ("GET", "/api/v1/records/7"),
        ("POST", "/api/v1/records"),
        ("PATCH", "/api/v1/records/7"),
        ("DELETE", "/api/v1/records/7"),
        ("GET", "/api/v1/monitoring/status"),
        ("GET", "/api/v1/monitoring/history"),
        ("POST", "/api/v1/auth/logout"),
    ]
    assert dict(requests[1].url.params) == {
        "category": "溫度 A & B",
        "page": "2",
        "page_size": "20",
    }
    assert json.loads(requests[3].content) == record
    assert json.loads(requests[4].content)["value"] == "0.000001"


@pytest.mark.parametrize("status", [401, 403, 404, 409, 422, 429, 500, 503])
def test_errors_are_safe_and_unauthorized_is_distinct(status):
    private = "password=private token=private-token SELECT secret"
    transport = httpx.MockTransport(lambda r: httpx.Response(status, text=private))
    with pytest.raises(APIError) as exc:
        APIClient("http://backend:8000", "private-token", transport=transport).me()
    assert exc.value.status_code == status
    assert exc.value.is_unauthorized == (status == 401)
    assert "private" not in str(exc.value)
    assert "SELECT" not in repr(exc.value)


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
def test_network_errors_are_sanitized(error):
    def fail(request):
        raise error("private connection detail", request=request)

    with pytest.raises(APIError) as exc:
        APIClient("http://backend:8000", transport=httpx.MockTransport(fail)).me()
    assert exc.value.status_code is None
    assert not exc.value.is_unauthorized
    assert "private" not in str(exc.value)


def test_redirect_is_not_followed_and_malformed_success_is_rejected():
    seen = []

    def redirect(request):
        seen.append(request)
        return httpx.Response(307, headers={"location": "http://untrusted.invalid"})

    with pytest.raises(APIError):
        APIClient("http://backend:8000", "private", transport=httpx.MockTransport(redirect)).me()
    assert len(seen) == 1
    for response in [httpx.Response(200, text="not json"), httpx.Response(200, json=[])]:
        with pytest.raises(APIError, match="回應格式"):
            APIClient(
                "http://backend:8000",
                transport=httpx.MockTransport(lambda r, result=response: result),
            ).me()


def test_malformed_login_does_not_create_an_authenticated_session():
    with pytest.raises(APIError, match="登入回應格式"):
        APIClient(
            "http://backend:8000",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"expires_in": 1})),
        ).login("user@example.com", "password")
