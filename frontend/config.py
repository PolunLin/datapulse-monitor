"""Separate container-side API requests from public browser links."""

import os
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class FrontendConfig:
    api_base_url: str
    public_api_url: str

    @property
    def swagger_url(self) -> str:
        public_url = self.public_api_url.rstrip("/")
        return public_url if public_url.endswith("/docs") else f"{public_url}/docs"

    @property
    def websocket_url(self) -> str:
        parsed = urlsplit(self.api_base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return parsed._replace(
            scheme=scheme, path=f"{parsed.path.rstrip('/')}/ws/v1/samples"
        ).geturl()


def _read_url(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} 必須是未含憑證或查詢參數的 HTTP／HTTPS 網址。")
    return value


def load_config() -> FrontendConfig:
    return FrontendConfig(
        api_base_url=_read_url("API_BASE_URL", "http://backend:8000"),
        public_api_url=_read_url("PUBLIC_API_URL", "http://localhost:8010"),
    )
