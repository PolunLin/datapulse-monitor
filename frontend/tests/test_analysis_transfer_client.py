"""Transport contracts for analysis, safe row errors and authenticated binary downloads."""

import httpx
import pytest

from clients.api import XLSX_MIME, APIClient, APIError, ImportValidationError


def test_analytics_and_export_send_same_filters_and_authorization():
    seen = []
    workbook = b"PK\x03\x04test-workbook"

    def handle(request):
        seen.append(request)
        if request.url.path.endswith("record-exports"):
            return httpx.Response(200, content=workbook, headers={"content-type": XLSX_MIME})
        return httpx.Response(200, json={"source": "records", "summary": {"total": "0.100001"}})

    client = APIClient("http://backend", "token", transport=httpx.MockTransport(handle))
    filters = {"source": "records", "category": "A & B", "start_time": "2026-11-08T00:00:00Z"}
    assert client.analytics(**filters, q=None)["summary"]["total"] == "0.100001"
    assert client.export_records(**filters) == workbook
    assert [r.url.path for r in seen] == ["/api/v1/analytics", "/api/v1/record-exports"]
    assert dict(seen[0].url.params) == dict(seen[1].url.params) == filters
    assert all(r.headers["authorization"] == "Bearer token" for r in seen)
    assert all(r.extensions["timeout"]["read"] == 30 for r in seen)


def test_import_sends_file_once_without_decimal_conversion():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(201, json={"imported_count": 1})

    client = APIClient("http://backend", "token", transport=httpx.MockTransport(handle))
    content = b'[{"title":"A","value":"999999999999.999999","category":"c"}]'
    assert client.import_records("data.json", content) == {"imported_count": 1}
    assert len(seen) == 1
    assert seen[0].url.path == "/api/v1/record-imports"
    assert seen[0].headers["content-type"].startswith("multipart/form-data;")
    assert b'name="file"; filename="data.json"' in seen[0].content
    assert content in seen[0].content


def test_structured_import_errors_are_allowlisted_without_echoing_backend_messages():
    private = "SELECT private token=private"
    body = {
        "error": {
            "code": "import_validation",
            "message": private,
            "details": {
                "errors": [
                    {"row": 2, "field": "value", "code": "invalid", "message": private},
                    {"row": 3, "field": private, "code": "invalid", "message": private},
                ],
                "total_errors": 2,
                "errors_truncated": False,
            },
        }
    }
    client = APIClient(
        "http://backend", transport=httpx.MockTransport(lambda r: httpx.Response(422, json=body))
    )
    with pytest.raises(ImportValidationError) as caught:
        client.import_records("data.csv", b"data")
    error = caught.value
    assert error.total_errors == 2 and error.truncated
    assert error.errors == [{"列": 2, "欄位": "數值", "原因": "內容不符合欄位格式或範圍。"}]
    assert private not in str(error) + repr(error.errors)


@pytest.mark.parametrize("body", [{}, {"error": "private"}, {"error": {"details": []}}])
def test_malformed_import_failure_uses_generic_safe_message(body):
    client = APIClient(
        "http://backend", transport=httpx.MockTransport(lambda r: httpx.Response(422, json=body))
    )
    with pytest.raises(APIError) as caught:
        client.import_records("x.csv", b"x")
    assert type(caught.value) is APIError
    assert "private" not in str(caught.value)


def test_local_size_limit_does_not_make_a_request():
    seen = []
    client = APIClient("http://backend", transport=httpx.MockTransport(seen.append))
    with pytest.raises(APIError) as caught:
        client.import_records("x.csv", b"x" * (2 * 1024 * 1024 + 1))
    assert caught.value.status_code == 413 and not seen


@pytest.mark.parametrize("status", [401, 403, 413, 422, 500])
def test_export_errors_are_safe_and_unauthorized_preserved(status):
    client = APIClient(
        "http://backend",
        transport=httpx.MockTransport(lambda r: httpx.Response(status, text="private token")),
    )
    with pytest.raises(APIError) as caught:
        client.export_records(source="realtime")
    assert caught.value.status_code == status
    assert caught.value.is_unauthorized == (status == 401)
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    ("content", "mime"), [(b"{}", "application/json"), (b"not-an-xlsx", XLSX_MIME)]
)
def test_export_rejects_malformed_binary_response(content, mime):
    client = APIClient(
        "http://backend",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=content, headers={"content-type": mime})
        ),
    )
    with pytest.raises(APIError, match="Excel 回應格式"):
        client.export_records()


def test_import_timeout_is_not_retried_automatically():
    seen = []

    def handle(request):
        seen.append(request)
        raise httpx.ReadTimeout("private", request=request)

    client = APIClient("http://backend", transport=httpx.MockTransport(handle))
    with pytest.raises(APIError) as caught:
        client.import_records("x.json", b"[]")
    assert caught.value.status_code is None
    assert len(seen) == 1
