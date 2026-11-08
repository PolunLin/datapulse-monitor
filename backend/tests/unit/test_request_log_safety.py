"""HTTP logging records safe route metadata instead of user-supplied request content."""

import json
import logging

import pytest
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.logging import JsonFormatter
from app.main import create_app


@pytest.mark.parametrize(
    "status_code, expected_level", [(200, "INFO"), (400, "WARNING"), (503, "ERROR")]
)
async def test_request_logs_exclude_body_headers_query_and_path_values(
    caplog, status_code, expected_level
):
    application = create_app(
        Settings(
            _env_file=None,
            database_url="mysql+asyncmy://test:test@localhost/logging_test",
            jwt_secret="logging-unit-test-secret-with-thirty-two-characters",
        )
    )

    @application.post("/diagnostic/{value}")
    async def diagnostic(value: str):
        return JSONResponse({"ok": True}, status_code=status_code)

    logger = logging.getLogger("app.requests")
    logger.addHandler(caplog.handler)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await client.post(
                "/diagnostic/path-secret?password=query-secret",
                headers={
                    "Authorization": "Bearer header-secret",
                    "X-Request-ID": "safe-request-id",
                },
                json={"password": "body-secret"},
            )
        assert response.status_code == status_code
        record = next(record for record in caplog.records if record.name == "app.requests")
        payload = json.loads(JsonFormatter().format(record))
        assert payload["path"] == "/diagnostic/{value}"
        assert payload["level"] == expected_level
        assert payload["request_id"] == response.headers["x-request-id"] == "safe-request-id"
        assert payload["duration_ms"] >= 0
        assert "secret" not in json.dumps(payload)
    finally:
        logger.removeHandler(caplog.handler)


async def test_unmatched_path_is_not_echoed_in_logs(caplog):
    application = create_app(
        Settings(
            _env_file=None,
            database_url="mysql+asyncmy://test:test@localhost/logging_test",
            jwt_secret="logging-unit-test-secret-with-thirty-two-characters",
        )
    )
    logger = logging.getLogger("app.requests")
    logger.addHandler(caplog.handler)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await client.get("/unmatched/credential-secret?token=query-secret")
        assert response.status_code == 404
        record = next(record for record in caplog.records if record.name == "app.requests")
        assert record.path == "[unmatched]"
        assert "secret" not in JsonFormatter().format(record)
    finally:
        logger.removeHandler(caplog.handler)


def test_json_formatter_exposes_exception_type_without_message_or_arguments():
    exception = RuntimeError("password-and-sql-parameters-secret")
    record = logging.LogRecord(
        "app.requests",
        logging.ERROR,
        __file__,
        1,
        "Database operation failed",
        (),
        (RuntimeError, exception, None),
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["error_type"] == "RuntimeError"
    assert "secret" not in json.dumps(payload)


async def test_included_router_logs_full_template_instead_of_leaf_path(caplog):
    application = create_app(
        Settings(
            _env_file=None,
            database_url="mysql+asyncmy://test:test@localhost/logging_test",
            jwt_secret="logging-unit-test-secret-with-thirty-two-characters",
        )
    )
    application.state.session_factory = None  # Missing credentials fail before database access.
    logger = logging.getLogger("app.requests")
    logger.addHandler(caplog.handler)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/records/42?token=secret")
        assert response.status_code == 401
        record = next(record for record in caplog.records if record.name == "app.requests")
        assert record.path == "/api/v1/records/{record_id}"
        assert "42" not in record.path
    finally:
        logger.removeHandler(caplog.handler)
