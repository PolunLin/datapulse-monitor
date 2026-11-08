"""HTTP pools belong to a login session and are released on every auth exit."""

import gc
from types import SimpleNamespace
from weakref import ref

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from clients.api import APIClient, APIError
from state import session
from views import auth


class TrackingTransport(httpx.MockTransport):
    def __init__(self, handler=None):
        self.requests = []
        self.close_calls = 0

        def handle(request):
            self.requests.append(request)
            return handler(request) if handler else httpx.Response(200, json={"id": 1})

        super().__init__(handle)

    def close(self):
        self.close_calls += 1
        super().close()


def test_http_pool_survives_requests_and_context_always_closes_it():
    transport = TrackingTransport()
    with pytest.raises(ValueError, match="caller failed"):
        with APIClient("http://api.test", "private", transport=transport) as client:
            client.me()
            client.list_records()
            assert transport.close_calls == 0
            assert len(transport.requests) == 2
            assert all(r.headers["Authorization"] == "Bearer private" for r in transport.requests)
            assert all(r.extensions["timeout"]["read"] == 8 for r in transport.requests)
            raise ValueError("caller failed")
    assert transport.close_calls == 1
    client.close()
    assert transport.close_calls == 1


def test_abandoned_session_client_releases_pool_when_collected():
    transport = TrackingTransport()
    client = APIClient("http://api.test", transport=transport)
    client.me()
    reference = ref(client)
    del client
    gc.collect()
    assert reference() is None
    assert transport.close_calls == 1


def test_feed_preserves_zero_cursor_and_omits_unset_filters():
    transport = TrackingTransport()
    with APIClient("http://api.test", "token", transport=transport) as client:
        client.monitoring_sample_feed(device_id="device-001", after_id=0, limit=300, q=None)
    request = transport.requests[0]
    assert request.url.path == "/api/v1/monitoring/sample-feed"
    assert dict(request.url.params) == {
        "device_id": "device-001",
        "after_id": "0",
        "limit": "300",
    }


@pytest.fixture
def session_clients(monkeypatch):
    state = {"auth_token": "token-one"}
    transports = []

    def make_client(*args, **kwargs):
        transport = TrackingTransport()
        transports.append(transport)
        return APIClient(*args, **kwargs, transport=transport)

    monkeypatch.setenv("API_BASE_URL", "http://api.test")
    monkeypatch.setattr(session, "st", SimpleNamespace(session_state=state))
    monkeypatch.setattr(session, "APIClient", make_client)
    yield state, transports
    session.close_api_client()


def test_session_reuses_client_and_closes_pool_before_token_or_backend_change(
    session_clients, monkeypatch
):
    state, transports = session_clients
    first = session.get_api_client()
    first.me()
    assert session.get_api_client() is first
    assert len(transports) == 1 and transports[0].close_calls == 0

    state["auth_token"] = "token-two"
    second = session.get_api_client()
    second.me()
    assert second is not first and transports[0].close_calls == 1
    assert transports[0].requests[0].headers["Authorization"] == "Bearer token-one"
    assert transports[1].requests[0].headers["Authorization"] == "Bearer token-two"

    monkeypatch.setenv("API_BASE_URL", "http://another.test")
    third = session.get_api_client()
    third.me()
    assert third is not second and transports[1].close_calls == 1
    assert transports[2].requests[0].url.host == "another.test"


def test_separate_session_states_have_independent_pools_and_credentials(
    session_clients, monkeypatch
):
    state, transports = session_clients
    first = session.get_api_client()
    first.me()
    other_state = {"auth_token": "token-two"}
    monkeypatch.setattr(session.st, "session_state", other_state)
    second = session.get_api_client()
    second.me()
    assert first is not second
    assert transports[0].requests[0].headers["Authorization"] == "Bearer token-one"
    assert transports[1].requests[0].headers["Authorization"] == "Bearer token-two"
    session.clear_session()
    assert not other_state and transports[1].close_calls == 1
    assert transports[0].close_calls == 0
    monkeypatch.setattr(session.st, "session_state", state)
    session.clear_session()
    assert transports[0].close_calls == 1


def test_clear_session_closes_pool_even_when_receiver_cleanup_fails(session_clients):
    state, transports = session_clients
    session.get_api_client().me()

    def fail():
        raise RuntimeError("receiver cleanup failed")

    state["realtime_receiver"] = SimpleNamespace(stop=fail)
    with pytest.raises(RuntimeError, match="receiver cleanup failed"):
        session.clear_session("已清除")
    assert state == {"auth_notice": "已清除"}
    assert transports[0].close_calls == 1


@pytest.mark.parametrize("exit_path", ["logout", "logout_failure", "expiry"])
def test_auth_exit_closes_pool_and_clears_private_state(session_clients, monkeypatch, exit_path):
    state, transports = session_clients
    client = session.get_api_client()
    client.me()

    class Rerun(Exception):
        pass

    def rerun():
        raise Rerun()

    monkeypatch.setattr(session.st, "rerun", rerun, raising=False)
    if exit_path == "logout_failure":

        def fail_logout():
            raise APIError(503, "safe error")

        monkeypatch.setattr(client, "logout", fail_logout)
    with pytest.raises(Rerun):
        if exit_path == "expiry":
            session.expire_session()
        else:
            session.logout()
    assert set(state) == {"auth_notice"}
    assert transports[0].close_calls == 1


@pytest.mark.parametrize("phase", ["login", "me", "register", "login_success"])
def test_auth_forms_close_temporary_pools_on_success_and_failure(monkeypatch, phase):
    transports = []

    def handler(request):
        if request.url.path.endswith("/login"):
            if phase == "login":
                return httpx.Response(401)
            return httpx.Response(200, json={"access_token": "token", "expires_in": 1800})
        if request.url.path.endswith("/register"):
            return httpx.Response(409)
        if phase == "me":
            return httpx.Response(503)
        return httpx.Response(200, json={"id": 1, "role": "user"})

    def make_client(*args, **kwargs):
        transport = TrackingTransport(handler)
        transports.append(transport)
        return APIClient(*args, **kwargs, transport=transport)

    monkeypatch.setenv("API_BASE_URL", "http://api.test")
    monkeypatch.setattr(auth, "APIClient", make_client)
    app = AppTest.from_string("from views.auth import render\nrender()", default_timeout=15).run()
    if phase == "register":
        app.text_input(key="register_email").set_value("user@example.com")
        app.text_input(key="register_password").set_value("private-password")
        next(item for item in app.text_input if item.label == "再次輸入密碼").set_value(
            "private-password"
        )
        next(button for button in app.button if button.label == "建立帳號").click().run()
    else:
        app.text_input(key="login_email").set_value("user@example.com")
        app.text_input(key="login_password").set_value("private-password")
        next(button for button in app.button if button.label == "登入").click().run()
    assert not app.exception
    assert len(transports) == (2 if phase in {"me", "login_success"} else 1)
    assert all(transport.close_calls == 1 for transport in transports)
