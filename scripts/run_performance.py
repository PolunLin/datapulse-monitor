"""Run bounded load/fault tests against a private, uniquely named Compose stack.

Use the backend development Python environment. Artifacts contain generated credentials
with mode 0600; share only the public JSON reports, never private.json/private.env.
No command targets the development project or removes images/global Docker resources.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.performance.yaml"


def write_json(path, value, *, private=False):
    temporary = path.with_suffix(path.suffix + ".tmp")
    if private:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, indent=2)
    else:
        temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def emit(event, **values):
    print(json.dumps({"event": event, "timestamp": time.time(), **values}), flush=True)


class Stack:
    def __init__(self, directory):
        self.directory = directory.resolve()
        self.private = json.loads((self.directory / "private.json").read_text())
        self.project = self.private["project"]
        if not re.fullmatch(r"datapulse-perf-[a-z0-9][a-z0-9-]{0,63}", self.project):
            raise ValueError("Refusing a project outside the disposable performance namespace")
        self.env = {**os.environ, **self.private["environment"]}
        self.command = [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--env-file",
            str(self.directory / "private.env"),
            "-f",
            str(COMPOSE),
        ]
        self.url = f"http://127.0.0.1:{self.private['port']}"

    def compose(self, *args, timeout=180, capture=True):
        result = subprocess.run(
            [*self.command, *args],
            env=self.env,
            text=True,
            capture_output=capture,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            # Docker diagnostic bodies aren't included in shareable reports.
            raise RuntimeError(f"Compose {args[0]} exited with {result.returncode}")
        return result.stdout if capture else ""

    def container(self, service):
        identifier = self.compose("ps", "-q", service).strip()
        if not identifier or "\n" in identifier:
            raise RuntimeError("Expected exactly one owned service container")
        return identifier

    def mark(self, scenario, phase):
        context = {"scenario": scenario, "phase": phase, "timestamp": time.time()}
        write_json(self.directory / "control.json", context)
        with (self.directory / "timeline.jsonl").open("a") as output:
            output.write(json.dumps(context) + "\n")
        emit("phase", **context)


def setup(directory, port):
    if port in (8010, 8510) or not 1024 <= port <= 65535:
        raise ValueError("Use a dedicated non-development port between 1024 and 65535")
    directory.mkdir(parents=True, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
    project = "datapulse-perf-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    project += "-" + secrets.token_hex(2)
    for resource_command in (["ps", "-aq"], ["volume", "ls", "-q"], ["network", "ls", "-q"]):
        result = subprocess.run(
            [
                "docker",
                *resource_command,
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        if result.stdout.strip():
            raise RuntimeError("Refusing an existing Compose project's resources")
    environment = {
        "PERF_DB_PASSWORD": secrets.token_urlsafe(30),
        "PERF_DB_ROOT_PASSWORD": secrets.token_urlsafe(30),
        "PERF_JWT_SECRET": secrets.token_urlsafe(48),
        "PERF_ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "PERF_IMAGE": f"{project}-backend:local",
        "PERF_BACKEND_PORT": str(port),
    }
    private = {"project": project, "port": port, "environment": environment}
    write_json(directory / "private.json", private, private=True)
    write_json(
        directory / "credentials.json",
        {
            "email": "performance@example.com",
            "password": environment["PERF_ADMIN_PASSWORD"],
        },
        private=True,
    )
    descriptor = os.open(directory / "private.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write("\n".join(f"{key}={value}" for key, value in environment.items()) + "\n")
    stack = Stack(directory)
    metadata = {
        "project": project,
        "created_utc": datetime.now(UTC).isoformat(),
        "backend_cpu_limit": 2,
        "backend_memory_limit_bytes": 2 * 1024**3,
        "database_cpu_limit": 2,
        "database_memory_limit_bytes": 2 * 1024**3,
        "backend_workers": 1,
        "db_pool_size": 5,
        "db_max_overflow": 5,
        "ws_max_connections": 0,
        "ws_queue_capacity": 100,
        "ws_auth_check_interval_seconds": 1,
        "ws_send_timeout_seconds": 2,
        "simulator_interval_seconds": 1,
        "simulator_devices": 3,
        "load_generator": "host Python asyncio; shared physical host with Docker Desktop",
        "scope": "backend REST, WebSocket, persistence and recovery; no browser capacity claim",
    }
    docker_info = (
        subprocess.run(
            ["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}} {{.Architecture}}"],
            text=True,
            capture_output=True,
            check=True,
        )
        .stdout.strip()
        .split()
    )
    metadata["docker_host"] = dict(
        zip(("cpus", "memory_bytes", "architecture"), docker_info, strict=True)
    )
    metadata["source_sha256"] = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for parent in (ROOT / "backend/app", ROOT / "backend/migrations")
        for path in sorted(parent.rglob("*.py"))
    }
    write_json(directory / "metadata.json", metadata)
    emit("setup_started", project=project, directory=str(directory))
    stack.compose("build", "migrate", timeout=600, capture=False)
    stack.compose("up", "-d", "--wait", "--wait-timeout", "180", timeout=240, capture=False)
    emit("setup_ready", project=project, api_url=stack.url)


async def login(stack, client):
    credentials = json.loads((stack.directory / "credentials.json").read_text())
    response = await client.post(
        stack.url + "/api/v1/auth/login",
        data={
            "username": credentials["email"],
            "password": credentials["password"],
        },
    )
    response.raise_for_status()
    return response.json()["access_token"]


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * fraction))]


async def http_capacity(stack, rates, duration):
    """One request per scheduled iteration; do not reduce arrivals when server slows."""
    output = {
        "stages": [],
        "scope": "authenticated sample-feed reads, one request per iteration",
    }
    async with httpx.AsyncClient(
        timeout=5,
        trust_env=False,
        limits=httpx.Limits(
            max_connections=500,
            max_keepalive_connections=100,
        ),
    ) as client:
        token = await login(stack, client)
        headers = {"Authorization": "Bearer " + token}
        for rate in rates:
            latencies, statuses, tasks = [], Counter(), set()
            scheduled = 0
            dropped = 0
            scheduler_lateness = []
            started = time.monotonic()

            async def request(statuses=statuses, latencies=latencies):
                before = time.monotonic()
                try:
                    response = await client.get(
                        stack.url + "/api/v1/monitoring/sample-feed",
                        params={"device_id": "device-001", "limit": 30},
                        headers=headers,
                    )
                    code = str(response.status_code)
                    if response.status_code == 200 and "items" not in response.json():
                        code = "invalid_body"
                except Exception as exc:
                    code = type(exc).__name__
                statuses[code] += 1
                latencies.append((time.monotonic() - before) * 1000)

            for index in range(int(rate * duration)):
                deadline = started + index / rate
                await asyncio.sleep(max(0, deadline - time.monotonic()))
                scheduled += 1
                lateness = time.monotonic() - deadline
                scheduler_lateness.append(lateness * 1000)
                if len(tasks) >= 500 or lateness > max(0.1, 2 / rate):
                    dropped += 1
                    continue
                task = asyncio.create_task(request())
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            await asyncio.gather(*tasks)
            elapsed = time.monotonic() - started
            success = statuses.get("200", 0) / scheduled
            stage = {
                "target_rps": rate,
                "duration_seconds": duration,
                "scheduled": scheduled,
                "completed": sum(statuses.values()),
                "dropped_by_generator": dropped,
                "elapsed_seconds_including_drain": elapsed,
                "achieved_successful_rps": statuses.get("200", 0) / elapsed,
                "success_ratio": success,
                "scheduler_lateness_p95_ms": percentile(scheduler_lateness, 0.95),
                "statuses": dict(statuses),
                "latency_ms": {
                    "p50": percentile(latencies, 0.5),
                    "p95": percentile(latencies, 0.95),
                    "p99": percentile(latencies, 0.99),
                },
                "stable": (
                    success >= 0.99
                    and statuses.get("200", 0) / elapsed >= rate * 0.95
                    and (percentile(latencies, 0.95) or 1e9) < 1000
                ),
            }
            output["stages"].append(stage)
            write_json(stack.directory / "http-capacity.json", output)
            emit("http_stage", **stage)
            if success < 0.75:
                break
            await asyncio.sleep(5)


async def availability(stack):
    # Observer runs independently from fault commands and remains alive during restarts.
    stack.mark("baseline", "healthy")
    observer = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "scripts/availability_probe.py"),
        "--api-url",
        stack.url,
        "--credentials-file",
        str(stack.directory / "credentials.json"),
        "--output",
        str(stack.directory / "availability.json"),
        "--control-file",
        str(stack.directory / "control.json"),
        "--duration",
        "390",
        "--ws-clients",
        "10",
    )

    async def compose(*args, timeout=90):
        return await asyncio.to_thread(stack.compose, *args, timeout=timeout)

    def backend_state():
        return json.loads(
            subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    '{"restart_count":{{.RestartCount}},"oom_killed":{{.State.OOMKilled}},'
                    '"status":"{{.State.Status}}"}',
                    stack.container("backend"),
                ],
                text=True,
                capture_output=True,
                check=True,
            ).stdout
        )

    snapshots = []
    committed_ids = set()

    async def snapshot(client, headers, label):
        row = {"timestamp": time.time(), "label": label}
        try:
            status = await client.get(stack.url + "/api/v1/monitoring/status", headers=headers)
            status.raise_for_status()
            row["pipeline"] = status.json()
            history = await client.get(
                stack.url + "/api/v1/monitoring/history", params={"page_size": 20}, headers=headers
            )
            history.raise_for_status()
            row["history_total"] = history.json()["total"]
            identifiers = [item["event_id"] for item in history.json()["items"]]
            row["committed_event_ids"] = identifiers
            committed_ids.update(identifiers)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            row["error_type"] = type(exc).__name__
        snapshots.append(row)
        write_json(stack.directory / "pipeline-snapshots.json", snapshots)

    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            token = await login(stack, client)
            headers = {"Authorization": "Bearer " + token}
            await asyncio.sleep(25)
            await snapshot(client, headers, "baseline")
            stack.mark("websocket_toggle", "fault_start")
            response = await client.put(
                stack.url + "/api/v1/monitoring/websocket",
                headers=headers,
                json={"enabled": False},
            )
            response.raise_for_status()
            await asyncio.sleep(20)
            response = await client.put(
                stack.url + "/api/v1/monitoring/websocket",
                headers=headers,
                json={"enabled": True},
            )
            response.raise_for_status()
            stack.mark("websocket_toggle", "fault_removed")
            await asyncio.sleep(30)
            await snapshot(client, headers, "before_graceful_restart")
            stack.mark("graceful_restart", "fault_start")
            await compose("restart", "backend")
            stack.mark("graceful_restart", "restart_command_completed")
            await asyncio.sleep(40)
            await snapshot(client, headers, "after_graceful_restart")
            stack.mark("database_pause", "fault_start")
            await compose("pause", "db")
            try:
                await asyncio.sleep(30)
            finally:
                await compose("unpause", "db")
                stack.mark("database_pause", "fault_removed")
            await asyncio.sleep(55)
            await snapshot(client, headers, "after_database_recovery")
            before_crash = await asyncio.to_thread(backend_state)
            stack.mark("backend_crash", "fault_start")
            # Kill the actual Uvicorn process inside this owned container. Unlike
            # docker stop/kill, this exercises the runtime's restart policy.
            crash = (
                "import os,signal; from pathlib import Path; "
                "p=Path('/proc/1/task/1/children').read_text().split(); "
                "assert len(p)==1; "
                "assert b'uvicorn' in Path('/proc',p[0],'cmdline').read_bytes(); "
                "os.kill(int(p[0]), signal.SIGKILL)"
            )
            signal_command_error = None
            try:
                await compose("exec", "-T", "backend", "python", "-c", crash)
            except RuntimeError as exc:
                # Exec may be terminated as init exits; restart evidence below is authoritative.
                signal_command_error = type(exc).__name__
            stack.mark("backend_crash", "signal_delivered")
            await asyncio.sleep(55)
            after_crash = await asyncio.to_thread(backend_state)
            write_json(
                stack.directory / "crash-evidence.json",
                {
                    "before": before_crash,
                    "after": after_crash,
                    "signal_command_error_type": signal_command_error,
                    "automatic_restart_verified": after_crash["restart_count"]
                    > before_crash["restart_count"],
                },
            )
            await snapshot(client, headers, "after_crash_recovery")
            checks = []
            for identifier in sorted(committed_ids):
                response = await client.get(
                    stack.url + "/api/v1/monitoring/history",
                    params={"event_id": identifier},
                    headers=headers,
                )
                checks.append(
                    {
                        "event_id": identifier,
                        "confirmed": response.status_code == 200
                        and response.json().get("total") == 1,
                    }
                )
            write_json(
                stack.directory / "committed-event-verification.json",
                {
                    "sampled_previously_committed": len(checks),
                    "confirmed": sum(row["confirmed"] for row in checks),
                    "events": checks,
                },
            )
            stack.mark("final", "observation")
            await asyncio.wait_for(observer.wait(), timeout=180)
            if observer.returncode:
                raise RuntimeError("Availability observer failed")
    finally:
        # A cancelled driver must never leave the database frozen.
        try:
            await compose("unpause", "db", timeout=20)
        except (RuntimeError, subprocess.SubprocessError, OSError):
            pass
        if observer.returncode is None:
            observer.terminate()
            await observer.wait()


async def resource_monitor(stack, duration):
    start = time.monotonic()
    target = stack.directory / "resources.jsonl"
    while time.monotonic() - start < duration and not (stack.directory / "monitor.stop").exists():
        identifiers = await asyncio.to_thread(stack.compose, "ps", "-q", "backend", "db")
        identifiers = identifiers.split()
        if identifiers:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{json .}}",
                *identifiers,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            raw, _ = await process.communicate()
            with target.open("a") as output:
                for line in raw.decode().splitlines():
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    output.write(json.dumps({"timestamp": time.time(), **item}) + "\n")
        await asyncio.sleep(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "setup",
            "capacity",
            "http",
            "availability",
            "monitor",
            "inspect",
            "cleanup",
        ),
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=19110)
    parser.add_argument("--stages", default="100,200,400,800,1200,1600,2400,3200")
    parser.add_argument("--hold-seconds", type=int, default=45)
    parser.add_argument("--api-rps", type=int, default=5)
    parser.add_argument("--device-id")
    parser.add_argument("--duration", type=int, default=1800)
    args = parser.parse_args()
    if args.action == "setup":
        setup(args.run_dir, args.port)
        return
    stack = Stack(args.run_dir)
    if args.action == "capacity":
        command = [
            sys.executable,
            str(ROOT / "scripts/load_probe.py"),
            "--api-url",
            stack.url,
            "--credentials-file",
            str(stack.directory / "credentials.json"),
            "--output",
            str(stack.directory / "websocket-capacity.json"),
            "--stages",
            args.stages,
            "--hold-seconds",
            str(args.hold_seconds),
            "--api-rps",
            str(args.api_rps),
        ]
        if args.device_id:
            command.extend(["--device-id", args.device_id])
        subprocess.run(command, check=True)
    elif args.action == "http":
        asyncio.run(
            http_capacity(stack, [int(x) for x in args.stages.split(",")], args.hold_seconds)
        )
    elif args.action == "availability":
        asyncio.run(availability(stack))
    elif args.action == "monitor":
        asyncio.run(resource_monitor(stack, args.duration))
    elif args.action == "inspect":
        print(stack.compose("ps", "--all"))
        print(
            subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.Name}} restart={{.RestartCount}} "
                    "oom={{.State.OOMKilled}} status={{.State.Status}}",
                    stack.container("backend"),
                    stack.container("db"),
                ],
                text=True,
                capture_output=True,
                check=True,
            ).stdout
        )
    elif args.action == "cleanup":
        (stack.directory / "monitor.stop").touch()
        stack.compose("down", "--volumes", "--remove-orphans", timeout=120, capture=False)
        for name in ("private.json", "private.env", "credentials.json"):
            (stack.directory / name).unlink(missing_ok=True)
        emit("cleanup_completed", project=stack.project)


if __name__ == "__main__":
    main()
