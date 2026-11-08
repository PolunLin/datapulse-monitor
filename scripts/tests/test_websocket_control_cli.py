import importlib.util
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

script_path = Path(__file__).resolve().parents[1] / "websocket_control.py"
spec = importlib.util.spec_from_file_location("websocket_control", script_path)
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


@pytest.mark.parametrize(
    ("command", "method", "body", "enabled"),
    [
        ("start", "PUT", b'{"enabled":true}', True),
        ("stop", "PUT", b'{"enabled":false}', False),
        ("status", "GET", b"", False),
    ],
)
def test_control_authenticates_and_selects_command(command, method, body, enabled):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/api/v1/auth/login":
            assert parse_qs(request.content.decode()) == {
                "username": ["admin@example.com"],
                "password": ["private-password"],
            }
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"access_token": "private-token"})
        assert request.url.path == "/api/v1/monitoring/websocket"
        assert request.headers["authorization"] == "Bearer private-token"
        assert request.method == method
        assert request.content == body
        return httpx.Response(200, json={"enabled": enabled, "connections": 0})

    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle)) as client:
        state = controller.control(client, command, "admin@example.com", "private-password")
    assert state == {"enabled": enabled, "connections": 0}
    assert len(requests) == 2


@pytest.mark.parametrize("status", [401, 403, 500])
def test_failed_login_never_sends_control_request_or_prints_response(status):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, text="private-password private-token")

    with (
        httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle)) as client,
        pytest.raises(controller.ControlError) as error,
    ):
        controller.control(client, "stop", "admin@example.com", "private-password")
    assert len(requests) == 1
    assert "private-" not in str(error.value)


@pytest.mark.parametrize("status", [401, 403, 404, 503])
def test_control_rejection_is_safe(status):
    def handle(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"access_token": "private-token"})
        return httpx.Response(status, text="private-token private-password")

    with (
        httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle)) as client,
        pytest.raises(controller.ControlError) as error,
    ):
        controller.control(client, "stop", "admin@example.com", "private-password")
    assert "private-" not in str(error.value)
    if status == 403:
        assert "管理員" in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"enabled": "false", "connections": 0},
        {"enabled": False, "connections": -1},
        {"enabled": True, "connections": True},
    ],
)
def test_malformed_status_is_rejected(payload):
    def handle(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"access_token": "private-token"})
        return (
            httpx.Response(200, json=payload)
            if payload is not None
            else httpx.Response(200, text="private-token is not JSON")
        )

    with (
        httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle)) as client,
        pytest.raises(controller.ControlError) as error,
    ):
        controller.control(client, "status", "admin@example.com", "private-password")
    assert "private-token" not in str(error.value)


@pytest.mark.parametrize(
    "api_url",
    [
        "ftp://localhost:8010",
        "http://admin:secret@localhost",
        "http://@localhost",
        "http://localhost?token=secret",
        "http://localhost#secret",
        "http://localhost:99999",
        "http://[invalid",
        "http://local\nhost",
        "http://",
    ],
)
def test_invalid_api_urls_are_rejected(api_url):
    with pytest.raises(SystemExit) as error:
        controller.arguments(["status", "--api-url", api_url])
    assert error.value.code == 2


def test_environment_credentials_and_safe_success_output(monkeypatch, capsys):
    monkeypatch.setenv("DATAPULSE_EMAIL", "admin@example.com")
    monkeypatch.setenv("DATAPULSE_PASSWORD", "private-password")
    monkeypatch.setattr(controller.sys, "argv", ["websocket_control.py", "stop"])
    real_client = httpx.Client

    def handle(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"access_token": "private-token"})
        return httpx.Response(200, json={"enabled": False, "connections": 0})

    monkeypatch.setattr(
        controller.httpx,
        "Client",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handle)),
    )
    assert controller.main() == 0
    output = capsys.readouterr()
    assert "關閉" in output.out
    assert "0" in output.out
    assert "private-" not in output.out + output.err


@pytest.mark.parametrize(
    ("failure", "expected_code", "message"),
    [
        (httpx.ConnectError("private-password"), 1, "無法連線"),
        (httpx.ReadTimeout("private-token"), 1, "逾時"),
        (KeyboardInterrupt(), 130, "中止"),
    ],
)
def test_main_handles_failures_without_exposing_secrets(
    monkeypatch, capsys, failure, expected_code, message
):
    monkeypatch.setenv("DATAPULSE_EMAIL", "admin@example.com")
    monkeypatch.setenv("DATAPULSE_PASSWORD", "private-password")
    monkeypatch.setattr(controller.sys, "argv", ["websocket_control.py", "stop"])

    def fail(*_args):
        raise failure

    monkeypatch.setattr(controller, "run", fail)
    assert controller.main() == expected_code
    output = capsys.readouterr()
    assert message in output.out + output.err
    assert "private-" not in output.out + output.err
