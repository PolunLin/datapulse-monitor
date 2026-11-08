"""Receive real WebSocket samples and optionally confirm their committed history.

Run with the backend environment, for example:
    uv run --project backend python scripts/realtime_demo.py --verify-history

Credentials come from DATAPULSE_EMAIL/DATAPULSE_PASSWORD or interactive prompts.
This command never reads .env and never prints passwords or access tokens.
"""

import argparse
import asyncio
import getpass
import json
import os
import sys
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake


def positive_count(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 10000:
        raise argparse.ArgumentTypeError("count must be between 1 and 10000")
    return number


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Observe DataPulse live samples.")
    parser.add_argument(
        "--api-url", default=os.getenv("DATAPULSE_API_URL", "http://localhost:8010")
    )
    parser.add_argument("--count", type=positive_count, default=5)
    parser.add_argument("--category", help="Receive only this category (default: all).")
    parser.add_argument(
        "--device-id", help="Receive one device, e.g. device-001 (default: all)."
    )
    parser.add_argument(
        "--verify-history",
        action="store_true",
        help="Confirm samples were committed using the authenticated monitoring history.",
    )
    options = parser.parse_args()
    url = urlsplit(options.api_url)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        parser.error(
            "--api-url must be an HTTP(S) URL without credentials, query or fragment"
        )
    if options.category is not None:
        options.category = options.category.strip()
        if not 1 <= len(options.category) <= 100:
            parser.error("--category must contain 1 to 100 characters")
    if options.device_id is not None:
        import re

        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", options.device_id):
            parser.error(
                "--device-id must contain 1 to 64 letters, digits, underscores or hyphens"
            )
    options.api_url = options.api_url.rstrip("/")
    return options


def websocket_url(
    api_url: str, category: str | None, device_id: str | None = None
) -> str:
    url = urlsplit(api_url)
    filters = {}
    if category:
        filters["category"] = category
    if device_id:
        filters["device_id"] = device_id
    return urlunsplit(
        (
            "wss" if url.scheme == "https" else "ws",
            url.netloc,
            f"{url.path.rstrip('/')}/ws/v1/samples",
            urlencode(filters),
            "",
        )
    )


async def verify_history(client: httpx.AsyncClient, event_ids: set[str]) -> None:
    pending = set(event_ids)
    deadline = asyncio.get_running_loop().time() + 10.0
    while pending and asyncio.get_running_loop().time() < deadline:
        for event_id in list(pending):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            response = await client.get(
                "/api/v1/monitoring/samples",
                params={"event_id": event_id},
                timeout=min(remaining, 5.0),
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"History query failed (HTTP {response.status_code})."
                )
            items = response.json().get("items", [])
            if any(item.get("event_id") == event_id for item in items):
                pending.remove(event_id)
        if pending:
            await asyncio.sleep(
                min(0.2, max(0, deadline - asyncio.get_running_loop().time()))
            )
    if pending:
        raise RuntimeError(
            f"{len(pending)} sample(s) were not visible in history within 10 seconds. "
            "Check batch settings and the monitoring status endpoint."
        )
    print(f"Verified {len(event_ids)} sample(s) in committed MariaDB history.")


async def run(options: argparse.Namespace, email: str, password: str) -> None:
    async with httpx.AsyncClient(
        base_url=options.api_url, timeout=5.0, trust_env=False
    ) as client:
        response = await client.post(
            "/api/v1/auth/login", data={"username": email, "password": password}
        )
        if response.status_code != 200:
            raise RuntimeError(f"Login failed (HTTP {response.status_code}).")
        token = response.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Login response did not contain an access token.")
        client.headers["Authorization"] = f"Bearer {token}"
        received: set[str] = set()
        async with connect(
            websocket_url(options.api_url, options.category, options.device_id),
            additional_headers={"Authorization": f"Bearer {token}"},
            open_timeout=5,
            close_timeout=3,
            max_size=16384,
            max_queue=16,
            proxy=None,
        ) as websocket:
            while len(received) < options.count:
                message = await asyncio.wait_for(websocket.recv(), timeout=15.0)
                event = json.loads(message)
                if not isinstance(event, dict):
                    raise TypeError("Unexpected WebSocket message format.")
                if event.get("type") == "ready":
                    print("Connected; waiting for live samples. Ctrl+C stops the demo.")
                    continue
                if event.get("type") != "sample.created":
                    continue
                event_id = event.get("event_id")
                if not isinstance(event_id, str):
                    raise TypeError("Received a sample without an event ID.")
                if event_id in received:
                    continue
                received.add(event_id)
                print(
                    json.dumps(
                        {
                            key: event.get(key)
                            for key in (
                                "event_id",
                                "device_id",
                                "device_name",
                                "unit",
                                "category",
                                "value",
                                "threshold",
                                "is_anomaly",
                                "timestamp",
                                "persistence",
                            )
                        },
                        ensure_ascii=False,
                    )
                )

        print(
            f"Received {len(received)} sample(s); receipt alone does not confirm persistence."
        )
        if options.verify_history:
            await verify_history(client, received)


def main() -> int:
    options = arguments()
    try:
        email = os.getenv("DATAPULSE_EMAIL") or input("Email: ").strip()
        password = os.getenv("DATAPULSE_PASSWORD") or getpass.getpass("Password: ")
        if not email or not password:
            raise RuntimeError("Email and password are required.")
        asyncio.run(run(options, email, password))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except EOFError:
        print(
            "Provide credentials interactively or through DATAPULSE_EMAIL/PASSWORD.",
            file=sys.stderr,
        )
        return 1
    except TimeoutError:
        print(
            "Timed out waiting for the service or a sample; check simulator settings.",
            file=sys.stderr,
        )
        return 1
    except ConnectionClosed as exc:
        code = exc.rcvd.code if exc.rcvd else "unknown"
        print(
            f"WebSocket closed (code {code}). Reconnect with valid credentials.",
            file=sys.stderr,
        )
        return 1
    except InvalidHandshake:
        print(
            "WebSocket handshake was rejected; check credentials and the service.",
            file=sys.stderr,
        )
        return 1
    except (httpx.HTTPError, OSError):
        print("Unable to communicate with the service.", file=sys.stderr)
        return 1
    except (RuntimeError, TypeError, ValueError) as exc:
        # These messages describe protocol/status failures, never credentials or response bodies.
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
