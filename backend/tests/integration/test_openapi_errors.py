"""Documented error models match real authenticated API failures against MariaDB."""

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.repositories import records
from app.schemas.errors import ErrorResponse
from app.schemas.transfers import ImportErrorResponse
from app.services.transfers import MAX_UPLOAD_BYTES, XLSX_MEDIA_TYPE
from tests.conftest import PASSWORD

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def assert_documented_error(app, response, path, method, model=ErrorResponse):
    body = model.model_validate(response.json())
    assert body.request_id == response.headers["X-Request-ID"]
    schema = app.openapi()["paths"][path][method]["responses"][str(response.status_code)]
    assert schema["content"]["application/json"]["schema"] == {
        "$ref": f"#/components/schemas/{model.__name__}"
    }
    assert "X-Request-ID" in schema["headers"]
    return body


@pytest.mark.parametrize("failure", ["forbidden", "not_found", "conflict", "validation"])
async def test_authenticated_failures_use_the_documented_response(
    app, client, make_account, record_payload, failure
):
    account = await make_account("openapi-viewer@example.com", "viewer")
    if failure == "forbidden":
        path, method, code = "/api/v1/records", "post", 403
        response = await client.post(path, headers=account.headers, json=record_payload)
    elif failure == "not_found":
        path, method, code = "/api/v1/records/{record_id}", "get", 404
        response = await client.get("/api/v1/records/999999", headers=account.headers)
    elif failure == "conflict":
        path, method, code = "/api/v1/auth/register", "post", 409
        response = await client.post(path, json={"email": account.email, "password": PASSWORD})
    else:
        path, method, code = "/api/v1/records", "get", 422
        response = await client.get(path, headers=account.headers, params={"page": 0})
    assert response.status_code == code
    body = assert_documented_error(app, response, path, method)
    assert body.error.code == ("validation_error" if failure == "validation" else failure)


async def test_database_failure_matches_503_documentation(app, client, make_account, monkeypatch):
    account = await make_account("openapi-database@example.com")

    async def unavailable(*args, **kwargs):
        raise SQLAlchemyError("private database diagnostic")

    monkeypatch.setattr(records, "list_page", unavailable)
    response = await client.get("/api/v1/records", headers=account.headers)
    assert response.status_code == 503
    body = assert_documented_error(app, response, "/api/v1/records", "get")
    assert body.error.code == "database_unavailable"
    assert "private database diagnostic" not in response.text


@pytest.mark.parametrize("oversize", [False, True])
async def test_import_errors_keep_their_specialized_documentation(
    app, client, make_account, oversize
):
    account = await make_account("openapi-import@example.com")
    content = b"x" * (MAX_UPLOAD_BYTES + 1) if oversize else b"wrong\ncolumns\n"
    response = await client.post(
        "/api/v1/record-imports",
        headers=account.headers,
        files={"file": ("input.csv", content, "text/csv")},
    )
    assert response.status_code == (413 if oversize else 422)
    body = assert_documented_error(
        app, response, "/api/v1/record-imports", "post", ImportErrorResponse
    )
    assert body.error.details.errors


async def test_excel_response_still_uses_documented_binary_content(app, client, make_account):
    account = await make_account("openapi-excel@example.com")
    response = await client.get("/api/v1/record-exports", headers=account.headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == XLSX_MEDIA_TYPE
    assert response.content.startswith(b"PK")
    documented = app.openapi()["paths"]["/api/v1/record-exports"]["get"]["responses"]["200"]
    assert set(documented["content"]) == {XLSX_MEDIA_TYPE}
