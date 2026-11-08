"""Session-scoped, bounded REST requests. This module never calls Streamlit."""

from dataclasses import dataclass
from typing import Any
from weakref import finalize

import httpx


class APIError(Exception):
    """A user-safe failure; response bodies, credentials and URLs are not retained."""

    def __init__(self, status_code: int | None, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message

    @property
    def is_unauthorized(self) -> bool:
        return self.status_code == 401


class ImportValidationError(APIError):
    """Only allowlisted fields/codes from structured import failures reach the UI."""

    def __init__(self, errors: list[dict], total_errors: int, truncated: bool) -> None:
        super().__init__(422, "檔案驗證失敗，整批資料均未匯入。請修正後重新上傳。")
        self.errors = errors
        self.total_errors = total_errors
        self.truncated = truncated


_IMPORT_FIELDS = {
    "file": "檔案",
    "row": "整列",
    "title": "標題",
    "value": "數值",
    "category": "分類",
    "timestamp": "時間",
}
_IMPORT_REASONS = {
    "missing": "缺少必填欄位。",
    "invalid": "內容不符合欄位格式或範圍。",
    "extra": "含有不允許的欄位。",
    "format": "檔案格式不正確，請檢查 UTF-8 編碼與 CSV／JSON 結構。",
    "limit": "資料超過匯入筆數或欄位長度限制。",
    "empty": "檔案沒有可匯入的資料。",
    "duplicate": "欄位名稱重複。",
}


def _import_validation_error(response: httpx.Response) -> ImportValidationError | None:
    try:
        error = response.json()["error"]
        details = error["details"]
        if error["code"] != "import_validation" or not isinstance(details["errors"], list):
            return None
        errors = []
        for issue in details["errors"][:100]:
            row, field, code = issue["row"], issue["field"], issue["code"]
            if type(row) is not int or not 1 <= row <= 5002:
                continue
            if field not in _IMPORT_FIELDS or code not in _IMPORT_REASONS:
                continue
            errors.append({"列": row, "欄位": _IMPORT_FIELDS[field], "原因": _IMPORT_REASONS[code]})
        total = details["total_errors"]
        if not errors or type(total) is not int or not len(errors) <= total <= 100000:
            return None
        return ImportValidationError(errors, total, total > len(errors))
    except (ValueError, KeyError, TypeError):
        return None


_ERROR_MESSAGES = {
    400: "請求內容不正確，請檢查輸入資料。",
    401: "登入資訊不正確或已失效，請重新登入。",
    403: "目前帳號沒有執行此操作的權限。",
    404: "資料已不存在，請重新整理列表。",
    409: "資料發生衝突；註冊時請確認此 email 是否已被使用。",
    413: "檔案過大，請將檔案縮小至 2 MiB 以內。",
    422: "輸入資料格式不正確，請檢查必填欄位、數值精度與時間格式。",
    429: "操作過於頻繁，請稍後再試。",
}


class APIClient:
    """A bounded HTTP connection pool owned by one UI session or context manager."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=httpx.Timeout(8.0, connect=3.0),
            limits=httpx.Limits(
                max_connections=4, max_keepalive_connections=2, keepalive_expiry=30
            ),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        # Streamlit has no public browser-disconnect callback. Release the pool
        # when an abandoned session is collected, as well as on explicit logout.
        self._finalizer = finalize(self, self._client.close)

    def close(self) -> None:
        """Release connections; repeated cleanup is safe."""
        self._finalizer()

    def __enter__(self) -> "APIClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _request_response(
        self, method: str, path: str, *, import_errors: bool = False, **kwargs: Any
    ) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise APIError(None, "後端回應逾時，請稍後再試。") from None
        except httpx.HTTPError:
            raise APIError(None, "暫時無法連接後端，請確認服務狀態後再試。") from None

        if not 200 <= response.status_code < 300:
            if import_errors and response.status_code == 422:
                validation_error = _import_validation_error(response)
                if validation_error is not None:
                    raise validation_error
            message = _ERROR_MESSAGES.get(response.status_code)
            if message is None:
                message = "後端暫時無法處理請求，請稍後再試。"
            raise APIError(response.status_code, message)
        return response

    def _request(self, method: str, path: str, **kwargs: Any) -> dict | None:
        response = self._request_response(method, path, **kwargs)
        if response.status_code == 204:
            return None
        try:
            payload = response.json()
        except ValueError:
            raise APIError(None, "後端回應格式不正確，請稍後再試。") from None
        if not isinstance(payload, dict):
            raise APIError(None, "後端回應格式不正確，請稍後再試。")
        return payload

    def login(self, email: str, password: str) -> dict:
        payload = self._request(
            "POST", "/api/v1/auth/login", data={"username": email.strip(), "password": password}
        )
        if (
            not payload
            or not isinstance(payload.get("access_token"), str)
            or not payload["access_token"]
            or type(payload.get("expires_in")) is not int
            or payload["expires_in"] <= 0
        ):
            raise APIError(None, "登入回應格式不正確，請稍後再試。")
        return payload

    def register(self, email: str, password: str) -> dict:
        return self._request(
            "POST", "/api/v1/auth/register", json={"email": email.strip(), "password": password}
        )

    def me(self) -> dict:
        return self._request("GET", "/api/v1/users/me")

    def logout(self) -> None:
        self._request("POST", "/api/v1/auth/logout")

    def list_records(self, **filters: Any) -> dict:
        return self._request(
            "GET", "/api/v1/records", params={k: v for k, v in filters.items() if v is not None}
        )

    def get_record(self, record_id: int) -> dict:
        return self._request("GET", f"/api/v1/records/{record_id}")

    def create_record(self, payload: dict) -> dict:
        return self._request("POST", "/api/v1/records", json=payload)

    def update_record(self, record_id: int, payload: dict) -> dict:
        return self._request("PATCH", f"/api/v1/records/{record_id}", json=payload)

    def delete_record(self, record_id: int) -> None:
        self._request("DELETE", f"/api/v1/records/{record_id}")

    def monitoring_status(self) -> dict:
        return self._request("GET", "/api/v1/monitoring/status")

    def monitoring_devices(self) -> dict:
        return self._request("GET", "/api/v1/monitoring/devices")

    def monitoring_samples(self, **filters: Any) -> dict:
        return self._request(
            "GET",
            "/api/v1/monitoring/samples",
            params={k: v for k, v in filters.items() if v is not None},
        )

    def monitoring_sample_feed(self, **filters: Any) -> dict:
        return self._request(
            "GET",
            "/api/v1/monitoring/sample-feed",
            params={k: v for k, v in filters.items() if v is not None},
        )

    def admin_users(self, **filters: Any) -> dict:
        return self._request(
            "GET",
            "/api/v1/admin/users",
            params={k: v for k, v in filters.items() if v is not None},
        )

    def admin_user(self, user_id: int) -> dict:
        return self._request("GET", f"/api/v1/admin/users/{user_id}")

    def update_admin_user(self, user_id: int, payload: dict) -> dict:
        try:
            return self._request("PATCH", f"/api/v1/admin/users/{user_id}", json=payload)
        except APIError as exc:
            if exc.status_code == 409:
                raise APIError(
                    409, "無法變更自己的角色或停用自己的帳號，請由另一位管理員處理。"
                ) from None
            raise

    def admin_logs(self, **filters: Any) -> dict:
        return self._request(
            "GET",
            "/api/v1/admin/logs",
            params={k: v for k, v in filters.items() if v is not None},
        )

    def admin_logging_status(self) -> dict:
        return self._request("GET", "/api/v1/admin/logging-status")

    def admin_database(self) -> dict:
        return self._request("GET", "/api/v1/admin/database")

    def monitoring_history(self, **filters: Any) -> dict:
        return self._request(
            "GET",
            "/api/v1/monitoring/history",
            params={k: v for k, v in filters.items() if v is not None},
        )

    def analytics(self, **filters: Any) -> dict:
        return self._request(
            "GET",
            "/api/v1/analytics",
            params={k: v for k, v in filters.items() if v is not None},
            timeout=httpx.Timeout(30.0, connect=3.0),
        )

    def export_records(self, **filters: Any) -> bytes:
        response = self._request_response(
            "GET",
            "/api/v1/record-exports",
            params={k: v for k, v in filters.items() if v is not None},
            timeout=httpx.Timeout(30.0, connect=3.0),
        )
        if (
            response.headers.get("content-type", "").split(";")[0] != XLSX_MIME
            or not response.content.startswith(b"PK\x03\x04")
            or len(response.content) > 32 * 1024 * 1024
        ):
            raise APIError(None, "Excel 回應格式不正確，請稍後再試。")
        return response.content

    def import_records(self, filename: str, content: bytes) -> dict:
        if len(content) > 2 * 1024 * 1024:
            raise APIError(413, _ERROR_MESSAGES[413])
        payload = self._request(
            "POST",
            "/api/v1/record-imports",
            files={"file": (filename, content, "application/octet-stream")},
            import_errors=True,
            timeout=httpx.Timeout(30.0, connect=3.0),
        )
        if (
            not payload
            or type(payload.get("imported_count")) is not int
            or not 1 <= payload["imported_count"] <= 5000
        ):
            raise APIError(None, "匯入回應格式不正確，請先查詢資料確認結果。")
        return payload


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass(frozen=True)
class BackendStatus:
    ready: bool
    message: str


def get_backend_status(api_base_url: str) -> BackendStatus:
    try:
        response = httpx.get(
            f"{api_base_url.rstrip('/')}/health/ready",
            timeout=httpx.Timeout(3.0, connect=2.0),
            follow_redirects=False,
            trust_env=False,
        )
        if response.status_code == 200:
            payload = response.json()
            if isinstance(payload, dict) and payload.get("status") in {"ok", "ready"}:
                return BackendStatus(True, "後端服務與資料庫已就緒。")
            return BackendStatus(False, "後端健康檢查回應格式不符預期，請查看後端日誌。")
        if response.status_code == 503:
            return BackendStatus(False, "後端服務已啟動，資料庫或即時資料處理尚未就緒。")
        return BackendStatus(False, "暫時無法確認後端狀態，請查看服務日誌。")
    except httpx.TimeoutException:
        return BackendStatus(False, "後端回應逾時，請稍後重新整理。")
    except (httpx.HTTPError, ValueError):
        return BackendStatus(False, "暫時無法連接後端，請確認服務啟動後再重新整理。")
