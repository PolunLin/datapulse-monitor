"""Control WebSocket delivery without restarting the REST API.

Examples:
    uv run --project backend python scripts/websocket_control.py stop
    uv run --project backend python scripts/websocket_control.py start
    uv run --project backend python scripts/websocket_control.py status

Credentials come from DATAPULSE_EMAIL/DATAPULSE_PASSWORD or interactive prompts.
This command never reads .env or prints passwords, tokens, or response bodies.
"""

import argparse
import getpass
import os
import sys
from urllib.parse import urlsplit

import httpx

CONTROL_PATH = "api/v1/monitoring/websocket"


class ControlError(Exception):
    """A safe, user-facing error that contains no server-supplied content."""


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="即時開啟或關閉 WebSocket，REST API 持續運作。")
    parser.add_argument("command", choices=("start", "stop", "status"), help="開啟／關閉／查詢")
    parser.add_argument(
        "--api-url",
        default=os.getenv("DATAPULSE_API_URL", "http://localhost:8010"),
        help="後端網址（預設 http://localhost:8010）",
    )
    options = parser.parse_args(argv)
    try:
        url = urlsplit(options.api_url)
        valid = (
            url.scheme in {"http", "https"}
            and bool(url.hostname)
            and url.username is None
            and url.password is None
            and not url.query
            and not url.fragment
            and not any(character.isspace() for character in options.api_url)
        )
        # Accessing port also rejects malformed and out-of-range port values.
        _ = url.port
        _ = httpx.URL(options.api_url)
    except (ValueError, httpx.InvalidURL):
        valid = False
    if not valid:
        parser.error("--api-url 必須是 HTTP(S) 網址，不可包含帳密、查詢參數或片段。")
    options.api_url = options.api_url.rstrip("/")
    return options


def response_object(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ControlError("後端回應格式不正確。") from exc
    if not isinstance(payload, dict):
        raise ControlError("後端回應格式不正確。")
    return payload


def control(client: httpx.Client, command: str, email: str, password: str) -> dict[str, bool | int]:
    response = client.post("api/v1/auth/login", data={"username": email, "password": password})
    if response.status_code != 200:
        raise ControlError(f"登入失敗（HTTP {response.status_code}），請確認管理員帳密。")
    token = response_object(response).get("access_token")
    if not isinstance(token, str) or not token:
        raise ControlError("登入回應未包含有效的存取憑證。")
    client.headers["Authorization"] = f"Bearer {token}"

    if command == "status":
        response = client.get(CONTROL_PATH)
    else:
        response = client.put(CONTROL_PATH, json={"enabled": command == "start"}, timeout=30.0)
    if response.status_code == 403:
        raise ControlError("只有管理員可以查看或切換 WebSocket 狀態。")
    if response.status_code == 401:
        raise ControlError("登入憑證已失效，請重新執行指令。")
    if response.status_code != 200:
        raise ControlError(f"WebSocket 控制失敗（HTTP {response.status_code}）。")
    payload = response_object(response)
    enabled, connections = payload.get("enabled"), payload.get("connections")
    if type(enabled) is not bool or type(connections) is not int or connections < 0:
        raise ControlError("WebSocket 狀態回應格式不正確。")
    return {"enabled": enabled, "connections": connections}


def run(options: argparse.Namespace, email: str, password: str) -> None:
    with httpx.Client(base_url=options.api_url, timeout=5.0, trust_env=False) as client:
        state = control(client, options.command, email, password)
    label = "開啟" if state["enabled"] else "關閉"
    print(f"WebSocket 狀態：{label}；目前連線數：{state['connections']}。")


def main() -> int:
    options = arguments()
    try:
        email = (os.getenv("DATAPULSE_EMAIL") or input("管理員 Email：")).strip()
        password = os.getenv("DATAPULSE_PASSWORD") or getpass.getpass("管理員密碼：")
        if not email or not password:
            raise ControlError("請提供管理員 Email 與密碼。")
        run(options, email, password)
    except KeyboardInterrupt:
        print("\n已中止指令；可執行 status 確認目前狀態。")
        return 130
    except EOFError:
        print("請互動輸入帳密，或設定 DATAPULSE_EMAIL／DATAPULSE_PASSWORD。", file=sys.stderr)
        return 1
    except httpx.TimeoutException:
        print("等待後端逾時；可執行 status 確認目前狀態。", file=sys.stderr)
        return 1
    except (httpx.HTTPError, OSError):
        print("無法連線到後端，請確認服務與 --api-url。", file=sys.stderr)
        return 1
    except ControlError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
