"""Exercise the real REST and WebSocket clients used by the Streamlit frontend.

Run from the repository root:
    uv run --project frontend python scripts/frontend_smoke.py

Uses DATAPULSE_EMAIL / DATAPULSE_PASSWORD or prompts; never reads .env or prints
credentials. Creates and deletes one uniquely named QA record. Logout revokes
all existing tokens for the chosen account, so use a dedicated test account.
"""

import argparse
import getpass
import os
import sys
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontend"))

from clients.api import APIClient, APIError
from clients.realtime import RealtimeReceiver


class SmokeFailure(RuntimeError):
    """A safe, human-readable verification failure without request contents."""


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify frontend clients against a running DataPulse backend. "
        "Creates and deletes one QA record and logs out the chosen account."
    )
    parser.add_argument(
        "--api-url", default=os.getenv("DATAPULSE_API_URL", "http://localhost:8010")
    )
    parser.add_argument(
        "--count", type=int, default=3, help="Number of live samples (1–100)."
    )
    parser.add_argument(
        "--timeout", type=float, default=30, help="Live wait limit in seconds."
    )
    options = parser.parse_args()
    parsed = urlsplit(options.api_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        parser.error(
            "--api-url must be an HTTP(S) URL without credentials, query or fragment"
        )
    if not 1 <= options.count <= 100:
        parser.error("--count must be between 1 and 100")
    if not 1 <= options.timeout <= 300:
        parser.error("--timeout must be between 1 and 300 seconds")
    options.api_url = options.api_url.rstrip("/")
    return options


def websocket_url(api_url: str) -> str:
    parsed = urlsplit(api_url)
    return urlunsplit(
        (
            "wss" if parsed.scheme == "https" else "ws",
            parsed.netloc,
            f"{parsed.path.rstrip('/')}/ws/v1/samples",
            "",
            "",
        )
    )


def verify_crud(client: APIClient, user_id: int) -> None:
    marker = f"frontend-smoke-{uuid4()}"
    record_id = None
    deleted = False
    try:
        created = client.create_record(
            {"title": marker, "value": "25.123456", "category": "device-001"}
        )
        record_id = created["id"]
        if created["created_by"] != user_id:
            raise SmokeFailure("Created record has an unexpected owner.")
        loaded = client.get_record(record_id)
        if loaded["title"] != marker or Decimal(loaded["value"]) != Decimal(
            "25.123456"
        ):
            raise SmokeFailure("Record read did not preserve the submitted data.")
        updated = client.update_record(record_id, {"value": "42.654321"})
        if Decimal(updated["value"]) != Decimal("42.654321"):
            raise SmokeFailure("Record update did not preserve decimal precision.")
        page = client.list_records(
            q=marker,
            category="device-001",
            page=1,
            page_size=10,
            sort_by="id",
            sort_order="desc",
        )
        if page["total"] != 1 or page["items"][0]["id"] != record_id:
            raise SmokeFailure("Record filtering did not return the new record.")
        client.delete_record(record_id)
        deleted = True
        try:
            client.get_record(record_id)
        except APIError as exc:
            if exc.status_code != 404:
                raise
        else:
            raise SmokeFailure("Deleted record remained readable.")
        print(
            "PASS: create, read, update, filter and delete through the frontend REST client."
        )
    finally:
        if record_id is not None and not deleted:
            try:
                client.delete_record(record_id)
            except APIError as exc:
                if exc.status_code != 404:
                    print(
                        f"QA record {record_id} could not be removed; delete it after recovery.",
                        file=sys.stderr,
                    )


def verify_live(receiver: RealtimeReceiver, count: int, timeout: float) -> None:
    received = set()
    deadline = time.monotonic() + timeout
    while len(received) < count:
        receiver.touch()
        snapshot = receiver.snapshot()
        if snapshot.status in {"auth_failed", "error", "stopped"}:
            raise SmokeFailure(
                f"Realtime receiver ended with status {snapshot.status}."
            )
        for event in receiver.drain():
            if event.device_id not in {"device-001", "device-002", "device-003"}:
                raise SmokeFailure(
                    "Realtime sample has an unexpected simulated device ID."
                )
            if not event.device_name:
                raise SmokeFailure("Realtime sample is missing its device name.")
            if event.is_anomaly != (event.value > event.threshold):
                raise SmokeFailure(
                    "Realtime anomaly flag does not match the threshold."
                )
            if event.timestamp.utcoffset() is None:
                raise SmokeFailure("Realtime sample timestamp is missing its timezone.")
            received.add(event.event_id)
        if len(received) >= count:
            break
        if time.monotonic() >= deadline:
            raise SmokeFailure(
                "Timed out waiting for live samples; check simulator settings."
            )
        time.sleep(0.05)
    print(f"PASS: received {len(received)} validated device WebSocket sample(s).")


def verify_revoked(client: APIClient, receiver: RealtimeReceiver) -> None:
    try:
        client.me()
    except APIError as exc:
        if exc.status_code != 401:
            raise
    else:
        raise SmokeFailure("The logged-out token was still accepted by the REST API.")
    deadline = time.monotonic() + 8
    while receiver.snapshot().status != "auth_failed":
        receiver.touch()
        if time.monotonic() >= deadline:
            raise SmokeFailure(
                "The logged-out WebSocket was not rejected within 8 seconds."
            )
        time.sleep(0.05)
    if receiver.drain():
        raise SmokeFailure(
            "The receiver retained queued samples after authentication failure."
        )
    print(
        "PASS: logout revoked HTTP access and stopped the existing WebSocket subscription."
    )


def run(options: argparse.Namespace, email: str, password: str) -> None:
    public = APIClient(options.api_url)
    client = None
    receiver = None
    logged_out = False
    try:
        auth = public.login(email, password)
        client = APIClient(options.api_url, token=auth["access_token"])
        user = client.me()
        if user["role"] not in {"admin", "user"}:
            raise SmokeFailure("This CRUD smoke test requires a User or Admin account.")
        print("PASS: login and authenticated profile request.")
        verify_crud(client, user["id"])
        receiver = RealtimeReceiver(
            websocket_url(options.api_url), auth["access_token"]
        )
        receiver.start()
        verify_live(receiver, options.count, options.timeout)
        client.logout()
        logged_out = True
        verify_revoked(client, receiver)
    finally:
        if receiver is not None:
            receiver.stop()
        if client is not None and not logged_out:
            try:
                client.logout()
            except APIError:
                print("Server-side logout could not be confirmed.", file=sys.stderr)


def main() -> int:
    options = arguments()
    try:
        email = os.getenv("DATAPULSE_EMAIL") or input("Email: ").strip()
        password = os.getenv("DATAPULSE_PASSWORD") or getpass.getpass("Password: ")
        if not email or not password:
            raise SmokeFailure("Email and password are required.")
        run(options, email, password)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except EOFError:
        print(
            "Provide DATAPULSE_EMAIL/PASSWORD or interactive credentials.",
            file=sys.stderr,
        )
        return 1
    except APIError as exc:
        print(
            f"Frontend API verification failed (HTTP {exc.status_code or 'unavailable'}).",
            file=sys.stderr,
        )
        return 1
    except SmokeFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (KeyError, TypeError, ValueError, OSError):
        print(
            "Frontend verification encountered an invalid response or configuration.",
            file=sys.stderr,
        )
        return 1
    print("Frontend client smoke test passed; the QA record has been removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
