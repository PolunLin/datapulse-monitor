"""OpenAPI describes the public wire format, including specialized error responses."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app
from app.schemas.errors import ErrorResponse
from app.services.transfers import XLSX_MEDIA_TYPE


@pytest.fixture
def contract_app():
    application = create_app(
        Settings(
            _env_file=None,
            database_url="mysql+asyncmy://test:test@localhost/openapi_test",
            jwt_secret="openapi-contract-test-secret-at-least-thirty-two-characters",
        )
    )
    # Missing/invalid credentials and request-body validation do not perform DB I/O.
    application.state.session_factory = object()
    return application


def test_openapi_permissions_and_all_validation_responses_match_current_contract(contract_app):
    schema = contract_app.openapi()
    description = schema["info"]["description"]
    assert "All authenticated roles can analyze and export" in description
    assert "realtime analysis requires Admin" not in description
    assert "HTTPValidationError" not in json.dumps(schema)
    for path, operations in schema["paths"].items():
        if not path.startswith("/api/v1/"):
            continue
        for operation in operations.values():
            for code, response in operation["responses"].items():
                if int(code) < 400:
                    continue
                expected = (
                    "ImportErrorResponse"
                    if path == "/api/v1/record-imports" and code in {"413", "422"}
                    else "ErrorResponse"
                )
                assert response["content"]["application/json"]["schema"] == {
                    "$ref": f"#/components/schemas/{expected}"
                }
                assert "X-Request-ID" in response["headers"]
                if code == "401":
                    assert response["headers"]["WWW-Authenticate"]["schema"]["enum"] == ["Bearer"]


def test_openapi_preserves_import_details_excel_content_and_health_status(contract_app):
    schema = contract_app.openapi()
    paths = schema["paths"]
    upload = paths["/api/v1/record-imports"]["post"]
    assert upload["requestBody"]["required"] is True
    assert "multipart/form-data" in upload["requestBody"]["content"]
    issue = schema["components"]["schemas"]["ImportErrorBody"]["properties"]["details"]
    assert issue == {"$ref": "#/components/schemas/ImportIssueDetails"}
    export = paths["/api/v1/record-exports"]["get"]["responses"]["200"]
    assert export["content"] == {
        XLSX_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}
    }
    health = paths["/health/ready"]["get"]["responses"]["503"]
    assert health["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReadinessUnavailable"
    }
    # Bodyless operations remain bodyless; error documentation adds no success content.
    assert "content" not in paths["/api/v1/records/{record_id}"]["delete"]["responses"]["204"]


@pytest.mark.parametrize(
    "method,path,kwargs,status,code",
    [
        ("GET", "/api/v1/records", {}, 401, "unauthorized"),
        (
            "GET",
            "/api/v1/records",
            {"headers": {"Authorization": "Bearer invalid-token"}},
            401,
            "unauthorized",
        ),
        (
            "POST",
            "/api/v1/auth/register",
            {"json": {"email": "invalid-email", "password": "x"}},
            422,
            "validation_error",
        ),
        ("POST", "/api/v1/auth/login", {"data": {}}, 422, "validation_error"),
    ],
)
async def test_actual_auth_and_validation_errors_match_documented_envelope(
    contract_app, method, path, kwargs, status, code
):
    async with AsyncClient(
        transport=ASGITransport(app=contract_app), base_url="http://test"
    ) as client:
        response = await client.request(method, path, **kwargs)
    assert response.status_code == status
    body = ErrorResponse.model_validate(response.json())
    assert body.error.code == code
    assert body.request_id == response.headers["X-Request-ID"]
    documented = contract_app.openapi()["paths"][path][method.lower()]["responses"][str(status)]
    assert documented["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
    if status == 401:
        assert response.headers["WWW-Authenticate"] == "Bearer"
    else:
        assert body.error.details
        assert "input" not in json.dumps(response.json())
