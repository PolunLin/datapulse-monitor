"""Connection caps accept an explicit unlimited value without changing the default."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def configured_settings(**overrides):
    return Settings(
        _env_file=None,
        database_url="mysql+asyncmy://test:test@localhost/websocket_test",
        jwt_secret="websocket-unit-test-secret-at-least-thirty-two-characters",
        **overrides,
    )


def test_default_connection_limit_remains_one_hundred(monkeypatch):
    monkeypatch.delenv("WS_MAX_CONNECTIONS", raising=False)
    assert configured_settings().ws_max_connections == 100


def test_zero_environment_connection_limit_means_unlimited(monkeypatch):
    monkeypatch.setenv("WS_MAX_CONNECTIONS", "0")
    assert configured_settings().ws_max_connections == 0


@pytest.mark.parametrize("limit", [-1, 10001])
def test_connection_limit_rejects_values_outside_supported_range(limit):
    with pytest.raises(ValidationError):
        configured_settings(ws_max_connections=limit)
