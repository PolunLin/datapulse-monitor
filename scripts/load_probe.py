"""Measure REST and unfiltered WebSocket capacity without changing the target.

Credentials are read only from a private JSON file containing email/password.
Run with the backend Python environment. All timings use a monotonic clock except
sample age, which requires synchronized client/server wall clocks. This tool owns
no deployment resources and does not reconnect failed sockets during a stage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import resource
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

PATHS = (
    "/health/live",
    "/health/ready",
    "/api/v1/monitoring/sample-feed?device_id=device-001&limit=30",
)
CLOSE_REASONS = frozenset(
    {
        "Authentication temporarily unavailable",
        "Client is too slow",
        "Client event queue is full",
        "keepalive ping timeout",
        "Credentials expired or revoked",
    }
)


class Distribution:
    """A bounded random reservoir; percentiles are approximate for large runs."""

    def __init__(self, capacity=12000):
        self.capacity = capacity
        self.values = []
        self.count = 0
        self.total = 0.0
        self.maximum = 0.0
        self.random = random.Random(42)

    def add(self, value):
        self.count += 1
        self.total += value
        self.maximum = max(self.maximum, value)
        if len(self.values) < self.capacity:
            self.values.append(value)
        else:
            index = self.random.randrange(self.count)
            if index < self.capacity:
                self.values[index] = value

    def result(self):
        ordered = sorted(self.values)

        def percentile(p):
            return round(ordered[min(len(ordered) - 1, math.ceil(len(ordered) * p) - 1)], 3)

        return {
            "count": self.count,
            "reservoir_size": len(ordered),
            "mean": round(self.total / self.count, 3) if self.count else None,
            "p50": percentile(0.5) if ordered else None,
            "p95": percentile(0.95) if ordered else None,
            "p99": percentile(0.99) if ordered else None,
            "max": round(self.maximum, 3) if self.count else None,
        }


@dataclass
class EndpointStats:
    scheduled: int = 0
    completed: int = 0
    successful: int = 0
    dropped: int = 0
    statuses: Counter = field(default_factory=Counter)
    latency: Distribution = field(default_factory=Distribution)

    def result(self):
        return {
            "scheduled": self.scheduled,
            "completed": self.completed,
            "successful": self.successful,
            "dropped": self.dropped,
            "success_ratio": self.successful / self.scheduled if self.scheduled else 0,
            "statuses": dict(self.statuses),
            "latency_ms": self.latency.result(),
        }


@dataclass
class Stage:
    target: int
    connections_start: int = 0
    samples: int = 0
    duplicate_consecutive_events: int = 0
    malformed_messages: int = 0
    endpoints: dict = field(default_factory=lambda: {p: EndpointStats() for p in PATHS})
    lag: Distribution = field(default_factory=Distribution)
    loop_lag: Distribution = field(default_factory=Distribution)
    active_distribution: Distribution = field(default_factory=Distribution)
    close_codes: Counter = field(default_factory=Counter)
    close_reasons: Counter = field(default_factory=Counter)
    event_ids: dict = field(default_factory=dict)
    client_seconds: float = 0


@dataclass
class SocketState:
    active: bool = False
    last_sample: float = 0
    samples: int = 0
    previous_id: str | None = None


def emit(event, **values):
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def error_key(exc):
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return f"HTTP_{status}" if isinstance(status, int) else type(exc).__name__


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


class Probe:
    def __init__(self, args, token):
        self.args = args
        self.headers = {"Authorization": "Bearer " + token}
        parts = urlsplit(args.api_url)
        self.ws_url = urlunsplit(
            (
                "wss" if parts.scheme == "https" else "ws",
                parts.netloc,
                "/ws/v1/samples",
                urlencode({"device_id": args.device_id}) if args.device_id else "",
                "",
            )
        )
        self.states = []
        self.tasks = []
        self.stage = None
        self.stopping = False
        self.connection_times = Distribution()
        self.open_errors = Counter()
        self.ready_count = 0
        self.open_attempts = 0
        self.sampled_events = {}
        self.client = httpx.AsyncClient(
            base_url=args.api_url,
            headers=self.headers,
            timeout=args.timeout,
            trust_env=False,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )

    def active_count(self):
        return sum(state.active for state in self.states)

    async def reader(self, state):
        self.open_attempts += 1
        started = time.monotonic()
        accepted = False
        try:
            async with connect(
                self.ws_url,
                additional_headers=self.headers,
                open_timeout=self.args.timeout,
                close_timeout=0.5,
                ping_interval=20,
                ping_timeout=20,
                max_queue=16,
            ) as socket:
                message = json.loads(await asyncio.wait_for(socket.recv(), self.args.timeout))
                if message.get("type") != "ready":
                    self.open_errors["MissingReady"] += 1
                    return
                accepted = True
                state.active = True
                self.ready_count += 1
                self.connection_times.add((time.monotonic() - started) * 1000)
                async for raw in socket:
                    now = time.monotonic()
                    stage = self.stage
                    try:
                        message = json.loads(raw)
                        if message.get("type") != "sample.created":
                            continue
                        event_id = message["event_id"]
                        measured_at = datetime.fromisoformat(message["timestamp"])
                        age = (time.time() - measured_at.timestamp()) * 1000
                    except (ValueError, TypeError, KeyError, AttributeError):
                        if stage:
                            stage.malformed_messages += 1
                        continue
                    state.last_sample = now
                    state.samples += 1
                    if stage:
                        stage.samples += 1
                        stage.lag.add(age)
                        if state.previous_id == event_id:
                            stage.duplicate_consecutive_events += 1
                        if len(stage.event_ids) < 20:
                            stage.event_ids[event_id] = message.get("device_id")
                    state.previous_id = event_id
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not accepted:
                self.open_errors[error_key(exc)] += 1
            elif self.stage and not self.stopping:
                code = exc.code if isinstance(exc, ConnectionClosed) else error_key(exc)
                self.stage.close_codes[str(code)] += 1
                if isinstance(exc, ConnectionClosed):
                    reasons = (
                        getattr(exc.rcvd, "reason", None),
                        getattr(exc.sent, "reason", None),
                    )
                    reason = next((value for value in reasons if value in CLOSE_REASONS), "other")
                    self.stage.close_reasons[reason] += 1
        finally:
            state.active = False

    async def ramp(self, target):
        needed = max(0, target - self.active_count())
        attempts_start = self.open_attempts
        ready_start = self.ready_count
        errors_start = self.open_errors.copy()
        timings_start = self.connection_times.count
        start = time.monotonic()
        for index in range(needed):
            if index:
                await asyncio.sleep(
                    max(0, start + index / self.args.ramp_per_second - time.monotonic())
                )
            state = SocketState()
            self.states.append(state)
            self.tasks.append(asyncio.create_task(self.reader(state)))
            if index and index % 100 == 0:
                emit("ramp", target=target, active=self.active_count(), launched=index)
        await asyncio.sleep(0)
        deadline = time.monotonic() + self.args.timeout * 2
        while time.monotonic() < deadline:
            pending = self.open_attempts - attempts_start - (self.ready_count - ready_start)
            failed = sum((self.open_errors - errors_start).values())
            if pending <= failed:
                break
            await asyncio.sleep(0.1)
        return {
            "attempted": self.open_attempts - attempts_start,
            "ready": self.ready_count - ready_start,
            "errors": dict(self.open_errors - errors_start),
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "handshake_total_count": self.connection_times.count,
            "new_handshake_count": self.connection_times.count - timings_start,
            "handshake_ms_cumulative": self.connection_times.result(),
        }

    async def request(self, path, stage):
        stats = stage.endpoints[path]
        started = time.monotonic()
        try:
            response = await self.client.get(path)
            stats.statuses[str(response.status_code)] += 1
            if response.status_code == 200:
                body = response.json()
                valid = (
                    body.get("status") == "ok"
                    if path.startswith("/health/")
                    else isinstance(body.get("items"), list)
                )
                if valid:
                    stats.successful += 1
                else:
                    stats.statuses["InvalidBody"] += 1
        except Exception as exc:
            stats.statuses[error_key(exc)] += 1
        finally:
            stats.completed += 1
            stats.latency.add((time.monotonic() - started) * 1000)

    async def http_traffic(self, stage, duration, rps):
        tasks = set()
        started = time.monotonic()
        count = math.ceil(duration * rps)
        for index in range(count):
            due = started + index / rps
            await asyncio.sleep(max(0, due - time.monotonic()))
            path = PATHS[index % len(PATHS)]
            stats = stage.endpoints[path]
            stats.scheduled += 1
            # Requests cannot silently queue inside the generator and hide overload.
            if time.monotonic() - due > max(0.1, 2 / rps) or len(tasks) >= 48:
                stats.dropped += 1
                continue
            task = asyncio.create_task(self.request(path, stage))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        if tasks:
            await asyncio.gather(*tasks)

    async def observe(self, stage, duration):
        started = previous = time.monotonic()
        interval = 0.1
        index = 1
        next_progress = started + 30
        while time.monotonic() - started < duration:
            due = started + index * interval
            await asyncio.sleep(max(0, due - time.monotonic()))
            now = time.monotonic()
            active = self.active_count()
            stage.loop_lag.add(max(0, now - due) * 1000)
            stage.active_distribution.add(active)
            stage.client_seconds += active * (now - previous)
            if now >= next_progress:
                fresh = sum(s.active and now - s.last_sample <= 5 for s in self.states)
                emit(
                    "stage_progress",
                    target=stage.target,
                    active=active,
                    fresh=fresh,
                    elapsed_seconds=round(now - started, 3),
                )
                next_progress = now + 30
            previous = now
            index = max(index + 1, math.floor((now - started) / interval) + 1)

    async def measure(self, target):
        ramp_started_at_epoch = time.time()
        ramp = {} if self.args.http_only else await self.ramp(target)
        stage = Stage(target=target, connections_start=self.active_count())
        self.stage = stage
        started = time.monotonic()
        started_at_epoch = time.time()
        cpu_start = time.process_time()
        rps = target if self.args.http_only else self.args.api_rps
        emit("stage_start", target=target, active=stage.connections_start, api_rps=rps)
        await asyncio.gather(
            self.http_traffic(stage, self.args.hold_seconds, rps),
            self.observe(stage, self.args.hold_seconds),
        )
        duration = time.monotonic() - started
        finished_at_epoch = time.time()
        now = time.monotonic()
        active = self.active_count()
        fresh = sum(s.active and now - s.last_sample <= 5 for s in self.states)
        self.stage = None
        for event_id, device_id in stage.event_ids.items():
            if len(self.sampled_events) < 100:
                self.sampled_events[event_id] = device_id
        endpoints = {path: stats.result() for path, stats in stage.endpoints.items()}
        reasons = []
        for path, endpoint in endpoints.items():
            if endpoint["success_ratio"] < 0.99:
                reasons.append(f"api_success:{path}")
            if (endpoint["latency_ms"]["p95"] or math.inf) >= 1000:
                reasons.append(f"api_p95:{path}")
        loop = stage.loop_lag.result()
        if (loop["p95"] or 0) >= 100:
            reasons.append("generator_event_loop_lag")
        sample_lag = stage.lag.result()
        if not self.args.http_only:
            if active / target < 0.99:
                reasons.append("active_connections_below_99pct")
            if fresh / target < 0.99:
                reasons.append("fresh_connections_below_99pct")
            if (sample_lag["p95"] or math.inf) >= 2000:
                reasons.append("sample_age_p95_above_2s")
        result = {
            "target": target,
            "mode": "http_rps" if self.args.http_only else "websocket_connections",
            "duration_seconds": round(duration, 3),
            "ramp_started_at_epoch": ramp_started_at_epoch,
            "started_at_epoch": started_at_epoch,
            "finished_at_epoch": finished_at_epoch,
            "ramp": ramp,
            "connections_start": stage.connections_start,
            "connections_end": active,
            "fresh_connections_end": fresh,
            "connection_count": stage.active_distribution.result(),
            "samples_received": stage.samples,
            "samples_per_second": round(stage.samples / duration, 3),
            "samples_per_client_second": round(stage.samples / stage.client_seconds, 4)
            if stage.client_seconds
            else None,
            "sample_age_ms": sample_lag,
            "duplicate_consecutive_events": stage.duplicate_consecutive_events,
            "malformed_messages": stage.malformed_messages,
            "close_codes": dict(stage.close_codes),
            "close_reasons": dict(stage.close_reasons),
            "http_target_rps": rps,
            "http_actual_completed_rps": round(
                sum(s.completed for s in stage.endpoints.values()) / duration, 3
            ),
            "endpoints": endpoints,
            "generator": {
                "event_loop_lag_ms": loop,
                "cpu_one_core_percent": round(
                    (time.process_time() - cpu_start) / duration * 100, 2
                ),
                "max_rss_platform_units": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "socket_tasks_total": len(self.tasks),
            },
            "provisionally_stable": not reasons,
            "failure_reasons": reasons,
            "sampled_event_ids": list(stage.event_ids),
        }
        emit(
            "stage_complete",
            target=target,
            active=active,
            fresh=fresh,
            provisionally_stable=not reasons,
            failure_reasons=reasons,
            sample_age_p95_ms=sample_lag["p95"],
            generator_lag_p95_ms=loop["p95"],
        )
        return result

    async def close_sockets(self):
        self.stopping = True
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def verify_persistence(self):
        """Check sampled event IDs after delivery; pending is not a commit acknowledgement."""
        outstanding = dict(self.sampled_events)
        persisted = set()
        errors = Counter()
        deadline = time.monotonic() + 15
        while outstanding and time.monotonic() < deadline:
            for event_id in list(outstanding):
                try:
                    response = await self.client.get(
                        "/api/v1/monitoring/samples",
                        params={"event_id": event_id, "page_size": 1},
                    )
                    if response.status_code == 200:
                        if any(
                            item.get("event_id") == event_id for item in response.json()["items"]
                        ):
                            persisted.add(event_id)
                            del outstanding[event_id]
                    else:
                        errors[str(response.status_code)] += 1
                except Exception as exc:
                    errors[error_key(exc)] += 1
                if time.monotonic() >= deadline:
                    break
            if outstanding:
                await asyncio.sleep(1)
        return {
            "sampled": len(self.sampled_events),
            "persisted": len(persisted),
            "missing_event_ids": list(outstanding),
            "errors": dict(errors),
            "all_sampled_events_persisted": not outstanding,
            "verification_scope": "bounded delivered-event sample; not an exhaustive loss audit",
        }


async def run(args):
    fd_before = resource.getrlimit(resource.RLIMIT_NOFILE)
    desired_fds = max(args.stages) + 256 if not args.http_only else 256
    soft, hard = fd_before
    if soft < desired_fds:
        new_soft = desired_fds if hard == resource.RLIM_INFINITY else min(desired_fds, hard)
        resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
    credentials = json.loads(args.credentials_file.read_text())
    async with httpx.AsyncClient(timeout=args.timeout, trust_env=False) as client:
        response = await client.post(
            args.api_url + "/api/v1/auth/login",
            data={"username": credentials["email"], "password": credentials["password"]},
        )
        if response.status_code != 200:
            raise RuntimeError(f"Login returned HTTP {response.status_code}")
        token = response.json()["access_token"]
    credentials.clear()
    probe = Probe(args, token)
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "target": args.api_url,
        "configuration": {
            "stages": args.stages,
            "hold_seconds": args.hold_seconds,
            "ramp_per_second": args.ramp_per_second,
            "api_rps": args.api_rps,
            "http_only": args.http_only,
            "device_id": args.device_id,
            "traffic_profile": (
                "http_only"
                if args.http_only
                else "selected_device"
                if args.device_id
                else "all_devices"
            ),
            "timeout_seconds": args.timeout,
            "fd_limits_before": list(fd_before),
            "fd_limits": list(resource.getrlimit(resource.RLIMIT_NOFILE)),
            "stability_criteria": {
                "active_and_fresh_connections_min_ratio": 0.99,
                "freshness_seconds": 5,
                "each_endpoint_success_min_ratio": 0.99,
                "each_endpoint_p95_ms_less_than": 1000,
                "sample_age_p95_ms_less_than": 2000,
                "generator_loop_lag_p95_ms_less_than": 100,
            },
        },
        "limitations": [
            "Client and server wall clocks must be synchronized for sample age.",
            "Latency percentiles use bounded reservoirs, not all observations.",
            "Generator and target may share host resources; examine generator loop lag and CPU.",
            "No automatic socket reconnects; a failed connection remains failed within its stage.",
            "Application settings are not modified or verified by this probe.",
            "HTTP mix is round-robin across liveness, readiness and authenticated sample-feed.",
        ],
        "stages": [],
    }
    write_report(args.output, report)
    failures = 0
    try:
        for target in args.stages:
            result = await probe.measure(target)
            report["stages"].append(result)
            failures = failures + 1 if not result["provisionally_stable"] else 0
            write_report(args.output, report)
            severe_connection_loss = (
                not args.http_only and result["connections_end"] < target * 0.75
            )
            severe_api_loss = any(
                item["success_ratio"] < 0.5 for item in result["endpoints"].values()
            )
            if failures >= 2 or severe_connection_loss or severe_api_loss:
                report["stop_reason"] = "two_failing_stages_or_severe_connection_or_api_loss"
                break
        else:
            report["stop_reason"] = "configured_stages_completed_capacity_may_be_higher"
    finally:
        await probe.close_sockets()
        report["persistence_verification"] = await probe.verify_persistence()
        await probe.client.aclose()
        report["finished_at"] = datetime.now(UTC).isoformat()
        stable = [s["target"] for s in report["stages"] if s["provisionally_stable"]]
        report["highest_provisionally_stable_target"] = max(stable, default=0)
        write_report(args.output, report)
    emit(
        "complete",
        highest_provisionally_stable_target=report["highest_provisionally_stable_target"],
        stop_reason=report.get("stop_reason"),
        output=str(args.output),
    )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--credentials-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stages", default="100,200,400,800,1200,1600,2400,3200")
    parser.add_argument("--hold-seconds", default=45.0, type=float)
    parser.add_argument("--ramp-per-second", default=40.0, type=float)
    parser.add_argument("--api-rps", default=5.0, type=float)
    parser.add_argument("--device-id", help="Subscribe to one device; default receives all devices")
    parser.add_argument("--timeout", default=5.0, type=float)
    parser.add_argument("--http-only", action="store_true", help="Interpret stages as REST RPS")
    args = parser.parse_args()
    args.api_url = args.api_url.rstrip("/")
    args.stages = [int(value) for value in args.stages.split(",")]
    if not args.stages or any(value <= 0 for value in args.stages):
        parser.error("stages must be positive integers")
    if args.stages != sorted(set(args.stages)):
        parser.error("stages must be strictly increasing")
    if args.hold_seconds < 6 or min(args.ramp_per_second, args.api_rps, args.timeout) <= 0:
        parser.error("hold must be >=6 seconds and rates/timeouts must be positive")
    if args.credentials_file.stat().st_mode & 0o077:
        parser.error("credentials file must be private (chmod 600)")
    return args


if __name__ == "__main__":
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        emit("interrupted")
        sys.exit(130)
    except Exception as exc:
        # Never include response bodies, exception messages or credential values.
        emit("failed", error_type=type(exc).__name__)
        sys.exit(1)
