"""Observe an isolated DataPulse deployment while an operator injects faults.

This script never controls Docker or changes application data. Credentials are
read from a private JSON file with ``email`` and ``password`` keys. Its report
contains only selected metrics and identifiers, never tokens or response bodies.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import time
from collections import Counter, OrderedDict, deque
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percent / 100
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower), 3)


def event_time(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, OverflowError):
        return None


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as destination:
        json.dump(value, destination, indent=2, allow_nan=False)
        destination.write("\n")
    temporary.replace(path)


def websocket_url(api_url: str) -> str:
    parts = urlsplit(api_url)
    return urlunsplit(
        ("wss" if parts.scheme == "https" else "ws", parts.netloc, "/ws/v1/samples", "", "")
    )


def recovery_intervals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Require three consecutive successful observations before declaring recovery."""
    intervals = []
    first_failure = None
    last_failure = None
    recovery_start = None
    consecutive = 0
    for row in sorted(rows, key=lambda item: item["started_at"]):
        if not row["success"]:
            first_failure = first_failure or row["started_at"]
            last_failure = row["started_at"]
            recovery_start = None
            consecutive = 0
        elif first_failure is not None:
            recovery_start = recovery_start or row["started_at"]
            consecutive += 1
            if consecutive == 3:
                intervals.append(
                    {
                        "first_failure_at": first_failure,
                        "last_failure_at": last_failure,
                        "first_recovered_at": recovery_start,
                        "recovery_confirmed_at": row["finished_at"],
                        "observed_outage_seconds": round(recovery_start - first_failure, 3),
                        "gap_after_last_failure_seconds": round(recovery_start - last_failure, 3),
                    }
                )
                first_failure = last_failure = recovery_start = None
                consecutive = 0
    if first_failure is not None:
        intervals.append(
            {
                "first_failure_at": first_failure,
                "last_failure_at": last_failure,
                "first_recovered_at": recovery_start,
                "recovery_confirmed_at": None,
                "observed_outage_seconds": None,
            }
        )
    return intervals


class AvailabilityProbe:
    routes: ClassVar[dict[str, str]] = {
        "live": "/health/live",
        "ready": "/health/ready",
        "sample_feed": "/api/v1/monitoring/sample-feed?device_id=device-001&limit=30",
    }

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.started_at = time.time()
        self.stop = asyncio.Event()
        self.token = ""
        self.http_rows: list[dict[str, Any]] = []
        self.ws_transitions: list[dict[str, Any]] = []
        self.ws_clients = {
            index: {
                "connected": False,
                "ready": False,
                "samples": 0,
                "connections": 0,
                "last_sample_at": None,
                "max_sample_gap_seconds": 0.0,
                "lags_ms": deque(maxlen=2000),
            }
            for index in range(args.ws_clients)
        }
        self.inflight: dict[str, int] = Counter()
        self.request_tasks: set[asyncio.Task] = set()
        self.recent: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.critical: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.reservoir: list[dict[str, Any]] = []
        self.unique_events = 0
        self.random = random.Random(20261108)
        self.reconciliation: dict[str, Any] = {"status": "not_started"}

    def context(self) -> dict[str, Any]:
        if not self.args.control_file:
            return {"scenario": "unlabelled", "phase": "observation"}
        try:
            value = json.loads(self.args.control_file.read_text())
            if isinstance(value, dict):
                # Only explicitly recognised labels enter reports; arbitrary file
                # content and credentials never become metric labels.
                return {key: str(value[key])[:100] for key in ("scenario", "phase") if key in value}
        except (OSError, ValueError):
            pass
        return {"scenario": "unlabelled", "phase": "unknown"}

    def transition(self, index: int, kind: str, **details: Any) -> None:
        self.ws_transitions.append(
            {"at": time.time(), "client": index, "kind": kind, **self.context(), **details}
        )

    def sample(self, index: int, message: dict[str, Any]) -> None:
        now = time.time()
        client = self.ws_clients[index]
        if client["last_sample_at"] is not None:
            client["max_sample_gap_seconds"] = max(
                client["max_sample_gap_seconds"], now - client["last_sample_at"]
            )
        client["samples"] += 1
        client["last_sample_at"] = now
        timestamp = event_time(message.get("timestamp"))
        if timestamp is not None:
            client["lags_ms"].append((now - timestamp) * 1000)
        identifier = message.get("event_id")
        if not isinstance(identifier, str) or len(identifier) > 50:
            return
        if identifier not in self.recent:
            row = {
                "event_id": identifier,
                "event_at": timestamp,
                "first_seen_at": now,
                **self.context(),
            }
            self.unique_events += 1
            if len(self.reservoir) < 250:
                self.reservoir.append(row)
            else:
                slot = self.random.randrange(self.unique_events)
                if slot < 250:
                    self.reservoir[slot] = row
            self.recent[identifier] = row
            if len(self.recent) > 2000:
                self.recent.popitem(last=False)

    def protect_pending_candidates(self) -> None:
        for identifier, row in list(self.recent.items())[-20:]:
            self.critical[identifier] = row
            self.critical.move_to_end(identifier)
        while len(self.critical) > 250:
            self.critical.popitem(last=False)

    async def request(self, client: httpx.AsyncClient, route: str) -> None:
        self.inflight[route] += 1
        started = time.time()
        context = self.context()
        row: dict[str, Any] = {
            "route": route,
            "started_at": started,
            "success": False,
            **context,
        }
        try:
            response = await client.get(
                self.routes[route], headers={"Authorization": f"Bearer {self.token}"}
            )
            row["status"] = response.status_code
            if response.status_code == 200:
                body = response.json()
                if route == "sample_feed":
                    row["success"] = isinstance(body.get("items"), list)
                    timestamps = [
                        event_time(item.get("timestamp"))
                        for item in body.get("items", [])
                        if isinstance(item, dict)
                    ]
                    valid = [value for value in timestamps if value is not None]
                    row["latest_sample_age_seconds"] = (
                        round(time.time() - max(valid), 3) if valid else None
                    )
                    row["item_count"] = len(body.get("items", []))
                else:
                    row["success"] = body.get("status") == "ok"
        except asyncio.CancelledError:
            row["error_type"] = "CancelledError"
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError) as exc:
            row["error_type"] = type(exc).__name__
        finally:
            row["finished_at"] = time.time()
            row["latency_ms"] = round((row["finished_at"] - started) * 1000, 3)
            self.http_rows.append(row)
            self.inflight[route] -= 1

    async def http_loop(self, client: httpx.AsyncClient) -> None:
        next_tick = time.monotonic()
        while not self.stop.is_set():
            for route in self.routes:
                if self.inflight[route] >= 5:
                    self.http_rows.append(
                        {
                            "route": route,
                            "started_at": time.time(),
                            "finished_at": time.time(),
                            "success": False,
                            "error_type": "ProbeConcurrencyLimit",
                            "latency_ms": None,
                            **self.context(),
                        }
                    )
                    continue
                task = asyncio.create_task(self.request(client, route))
                self.request_tasks.add(task)
                task.add_done_callback(self.request_tasks.discard)
            next_tick += 1
            delay = max(0, next_tick - time.monotonic())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=delay)

    async def websocket_loop(self, index: int) -> None:
        client = self.ws_clients[index]
        delay = 0.5
        while not self.stop.is_set():
            self.transition(index, "connect_attempt")
            attempt = time.time()
            try:
                async with connect(
                    websocket_url(self.args.api_url),
                    additional_headers={"Authorization": f"Bearer {self.token}"},
                    open_timeout=5,
                    close_timeout=1,
                    ping_interval=20,
                    ping_timeout=10,
                    max_queue=32,
                ) as socket:
                    client["connected"] = True
                    client["ready"] = False
                    client["connections"] += 1
                    self.transition(index, "connected", connect_ms=(time.time() - attempt) * 1000)
                    first_sample = True
                    async for raw in socket:
                        message = json.loads(raw)
                        if message.get("type") == "ready":
                            client["ready"] = True
                            delay = 0.5
                            self.transition(index, "ready")
                        elif message.get("type") == "sample.created":
                            self.sample(index, message)
                            if first_sample:
                                self.transition(index, "first_sample")
                                first_sample = False
                        if self.stop.is_set():
                            break
                    if not self.stop.is_set():
                        self.protect_pending_candidates()
                        self.transition(index, "disconnected", close_code=socket.close_code)
            except asyncio.CancelledError:
                raise
            except (WebSocketException, OSError, TimeoutError, ValueError, TypeError) as exc:
                if not self.stop.is_set():
                    self.protect_pending_candidates()
                    details: dict[str, Any] = {"error_type": type(exc).__name__}
                    if isinstance(exc, ConnectionClosed) and exc.rcvd is not None:
                        details["close_code"] = exc.rcvd.code
                    response = getattr(exc, "response", None)
                    status = getattr(response, "status_code", None)
                    if isinstance(status, int):
                        details["http_status"] = status
                    self.transition(index, "disconnected", **details)
            finally:
                client["connected"] = client["ready"] = False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=delay)
            delay = min(delay * 2, 8)

    def report(self, status: str) -> dict[str, Any]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in self.http_rows:
            key = "|".join(
                (row.get("scenario", "unlabelled"), row.get("phase", "unknown"), row["route"])
            )
            grouped.setdefault(key, []).append(row)
        summary = {}
        for key, rows in grouped.items():
            latencies = [row["latency_ms"] for row in rows if row.get("latency_ms") is not None]
            successes = sum(row["success"] for row in rows)
            summary[key] = {
                "probes": len(rows),
                "successes": successes,
                "success_rate": successes / len(rows),
                "latency_p95_ms": percentile(latencies, 95),
                "latency_p99_ms": percentile(latencies, 99),
                "statuses": dict(Counter(str(row.get("status", "error")) for row in rows)),
                "errors": dict(Counter(row["error_type"] for row in rows if "error_type" in row)),
            }
        clients = {}
        for index, client in self.ws_clients.items():
            clients[index] = {
                **{key: value for key, value in client.items() if key != "lags_ms"},
                "lag_p95_ms": percentile(list(client["lags_ms"]), 95),
                "lag_p99_ms": percentile(list(client["lags_ms"]), 99),
                "lag_sample_window": len(client["lags_ms"]),
            }
        return {
            "schema_version": 1,
            "status": status,
            "started_at": self.started_at,
            "updated_at": time.time(),
            "configuration": {
                "duration_seconds": self.args.duration,
                "websocket_clients": self.args.ws_clients,
                "http_probe_interval_seconds": 1,
                "http_timeout_seconds": 3,
            },
            "http_summary": summary,
            "http_recovery_intervals": {
                route: recovery_intervals([row for row in self.http_rows if row["route"] == route])
                for route in self.routes
            },
            "http_observations": self.http_rows,
            "websocket_clients": clients,
            "websocket_transitions": self.ws_transitions,
            "unique_sample_events_observed": self.unique_events,
            "reconciliation": self.reconciliation,
        }

    async def progress_loop(self) -> None:
        while not self.stop.is_set():
            report = self.report("running")
            atomic_json(self.args.output, report)
            print(
                json.dumps(
                    {
                        "event": "availability_progress",
                        "at": time.time(),
                        "elapsed_seconds": round(time.time() - self.started_at, 1),
                        "connected": sum(value["ready"] for value in self.ws_clients.values()),
                        "http_probes": len(self.http_rows),
                        "unique_events": self.unique_events,
                        **self.context(),
                    }
                ),
                flush=True,
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=10)

    async def reconcile(self, client: httpx.AsyncClient) -> None:
        # Prefer the events seen immediately before disconnects. Those events are
        # most likely to expose the documented pending-data crash-loss window.
        candidates = {row["event_id"]: row for row in self.reservoir}
        candidates.update(self.critical)
        for identifier, row in list(self.recent.items())[-20:]:
            candidates[identifier] = row
        rows = list(candidates.values())[-500:]
        self.reconciliation = {
            "status": "settling",
            "candidate_count": len(rows),
            "settle_seconds": self.args.settle_seconds,
            "note": "Received events are pending; missing events after a crash may be lost.",
        }
        atomic_json(self.args.output, self.report("reconciling"))
        await asyncio.sleep(self.args.settle_seconds)
        gate = asyncio.Semaphore(4)

        async def check(row: dict[str, Any]) -> dict[str, Any]:
            result = dict(row)
            async with gate:
                try:
                    response = await client.get(
                        "/api/v1/monitoring/history",
                        params={"event_id": row["event_id"]},
                        headers={"Authorization": f"Bearer {self.token}"},
                    )
                    result["checked_at"] = time.time()
                    result["status"] = response.status_code
                    if response.status_code == 200:
                        items = response.json().get("items", [])
                        matches = [
                            item for item in items if item.get("event_id") == row["event_id"]
                        ]
                        result["outcome"] = "confirmed" if len(matches) == 1 else "missing_pending"
                        result["matching_rows"] = len(matches)
                    else:
                        result["outcome"] = "unverified"
                except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError) as exc:
                    result["outcome"] = "unverified"
                    result["error_type"] = type(exc).__name__
            return result

        checked = await asyncio.gather(*(check(row) for row in rows))
        self.reconciliation = {
            **self.reconciliation,
            "status": "complete",
            "outcomes": dict(Counter(row["outcome"] for row in checked)),
            "events": checked,
        }

    async def run(self) -> int:
        credentials = json.loads(self.args.credentials_file.read_text())
        async with httpx.AsyncClient(
            base_url=self.args.api_url.rstrip("/"),
            timeout=3,
            limits=httpx.Limits(max_connections=30, max_keepalive_connections=20),
            trust_env=False,
        ) as client:
            response = await client.post(
                "/api/v1/auth/login",
                data={"username": credentials["email"], "password": credentials["password"]},
                timeout=10,
            )
            if response.status_code != 200:
                raise RuntimeError(f"Initial login failed with HTTP {response.status_code}")
            self.token = response.json()["access_token"]
            del credentials
            tasks = [
                asyncio.create_task(self.http_loop(client)),
                asyncio.create_task(self.progress_loop()),
                *(asyncio.create_task(self.websocket_loop(index)) for index in self.ws_clients),
            ]
            try:
                await asyncio.sleep(self.args.duration)
            finally:
                self.stop.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if self.request_tasks:
                    await asyncio.gather(*list(self.request_tasks), return_exceptions=True)
                atomic_json(self.args.output, self.report("observation_complete"))
            await self.reconcile(client)
            atomic_json(self.args.output, self.report("complete"))
            print(
                json.dumps(
                    {
                        "event": "availability_complete",
                        "at": time.time(),
                        "http_probes": len(self.http_rows),
                        "reconciliation": self.reconciliation.get("outcomes", {}),
                        "output": str(self.args.output),
                    }
                ),
                flush=True,
            )
        return 0


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--credentials-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--duration", type=float, default=240)
    parser.add_argument("--control-file", type=Path)
    parser.add_argument("--ws-clients", type=int, default=10)
    parser.add_argument("--settle-seconds", type=float, default=10)
    args = parser.parse_args()
    parts = urlsplit(args.api_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username:
        parser.error("--api-url must be an HTTP(S) URL without embedded credentials")
    if not 0 < args.duration < 1500:
        parser.error("--duration must be between 0 and 1500 seconds (default token TTL is 30m)")
    if not 0 <= args.ws_clients <= 100:
        parser.error("--ws-clients must be between 0 and 100")
    if not 0 <= args.settle_seconds <= 60:
        parser.error("--settle-seconds must be between 0 and 60")
    if os.name == "posix" and args.credentials_file.stat().st_mode & 0o077:
        parser.error("--credentials-file must not be readable by other users (use mode 0600)")
    return args


def main() -> int:
    try:
        return asyncio.run(AvailabilityProbe(arguments()).run())
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - the CLI boundary must redact exception text.
        # Do not print exception text: HTTP exceptions can include request URLs,
        # headers or server response data. The type is sufficient for diagnosis.
        print(json.dumps({"event": "availability_failed", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
