"""Verify a disposable clean checkout through the real Compose startup chain.

Run with Python's standard library from a clone that has no .env:
    python3 scripts/clean_environment_smoke.py --project datapulse-clean-example

Creates private credentials, starts an isolated project, legally demotes its
bootstrap administrator, recreates the stack while retaining its volume, and
verifies persistence and permissions. Finally removes only resources it owns.
Never run against an existing deployment; existing .env or project resources
are rejected before ownership is established. No credentials are printed.
"""

import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
PROJECT_PATTERN = re.compile(r"datapulse-clean-[a-z0-9][a-z0-9-]{0,39}\Z")
WS_PROBE = """import json, sys
from websockets.sync.client import connect
credentials = json.load(sys.stdin)
with connect("ws://127.0.0.1:8000/ws/v1/samples?device_id=device-002",
             additional_headers={"Authorization": "Bearer " + credentials["token"]},
             open_timeout=5) as socket:
    ready = json.loads(socket.recv(timeout=10))
    assert ready["type"] == "ready"
    event = json.loads(socket.recv(timeout=10))
    assert event["type"] == "sample.created"
    print(json.dumps(event))
"""


class SmokeFailure(RuntimeError):
    """Only safe stage names and status codes; never process/request bodies."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def execute(args: list[str], *, cwd: Path, env=None, input_text=None, timeout=600) -> str:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SmokeFailure("A required process failed or timed out.") from exc
    if result.returncode:
        raise SmokeFailure(f"A required process exited with status {result.returncode}.")
    return result.stdout


class API:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.opener = build_opener(ProxyHandler({}))

    def request(self, method, path, *, token=None, payload=None, form=None, expected=200):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if form is not None:
            data = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=10) as response:
                status, body = response.status, response.read()
        except HTTPError as exc:
            status, body = exc.code, exc.read()
        except (URLError, TimeoutError, OSError) as exc:
            raise SmokeFailure("A service request failed or timed out.") from exc
        require(
            status == expected,
            f"{method} {path.split('?')[0]} returned {status}, expected {expected}.",
        )
        if not body:
            return None
        try:
            return json.loads(body)
        except (UnicodeError, ValueError) as exc:
            raise SmokeFailure("A service returned an unexpected response format.") from exc

    def login(self, email, password):
        return self.request(
            "POST", "/api/v1/auth/login", form={"username": email, "password": password}
        )["access_token"]


def wait_for(check, timeout=90):
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = check()
            if result:
                return result
        except SmokeFailure:
            pass
        if time.monotonic() >= deadline:
            raise SmokeFailure("Timed out waiting for a service or persisted sample.")
        time.sleep(1)


def read_generated_environment(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    for key in ("JWT_SECRET", "DB_PASSWORD", "DB_ROOT_PASSWORD", "BOOTSTRAP_ADMIN_PASSWORD"):
        require(
            bool(values.get(key)) and values[key] != "__GENERATE__",
            "Initialization did not generate credentials.",
        )
    return values


def ensure_unused_project(root: Path, project: str) -> None:
    require(bool(PROJECT_PATTERN.fullmatch(project)), "Use a unique datapulse-clean- project name.")
    for command in (
        ["docker", "ps", "-aq"],
        ["docker", "volume", "ls", "-q"],
        ["docker", "network", "ls", "-q"],
    ):
        output = execute(
            [*command, "--filter", f"label=com.docker.compose.project={project}"], cwd=root
        )
        require(
            not output.strip(), "This Compose project already has resources; nothing was changed."
        )


def ensure_ports_available(ports: tuple[int, int]) -> None:
    require(len(set(ports)) == 2, "Backend and frontend ports must differ.")
    for port in ports:
        require(
            1024 <= port <= 65535 and port not in {8010, 8510},
            "Choose alternate nonprivileged ports (not 8010/8510).",
        )
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise SmokeFailure("A selected port is already in use; nothing was changed.") from exc


class Deployment:
    def __init__(self, root: Path, project: str, backend_port: int, frontend_port: int):
        self.root, self.project = root, project
        self.backend_port, self.frontend_port = backend_port, frontend_port
        self.env_file = root / ".env"
        self.environment = None
        self.owns_env = False
        self.owns_resources = False

    def compose(self, *args, input_text=None):
        return execute(
            [
                "docker",
                "compose",
                "--project-name",
                self.project,
                "--env-file",
                str(self.env_file),
                "-f",
                str(self.root / "compose.yaml"),
                *args,
            ],
            cwd=self.root,
            env=self.environment,
            input_text=input_text,
        )

    def prepare(self):
        require(
            not self.env_file.exists() and not self.env_file.is_symlink(),
            "A clean checkout without .env is required; nothing was changed.",
        )
        ensure_ports_available((self.backend_port, self.frontend_port))
        ensure_unused_project(self.root, self.project)
        execute([sys.executable, "scripts/init_env.py"], cwd=self.root)
        self.owns_env = True
        values = read_generated_environment(self.env_file)
        self.environment = {
            key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")
        }
        self.environment.update(values)
        self.environment.update(
            BACKEND_PORT=str(self.backend_port),
            FRONTEND_PORT=str(self.frontend_port),
            PUBLIC_API_URL=f"http://localhost:{self.backend_port}",
        )
        return values

    def start(self, *, build):
        self.owns_resources = True
        self.compose("up", "-d", "--build" if build else "--no-build")
        api = API(f"http://127.0.0.1:{self.backend_port}")
        wait_for(lambda: api.request("GET", "/health/ready").get("status") == "ok")
        opener = build_opener(ProxyHandler({}))

        def frontend_ready():
            try:
                with opener.open(
                    f"http://127.0.0.1:{self.frontend_port}/_stcore/health", timeout=5
                ) as response:
                    return response.status == 200 and response.read().strip() == b"ok"
            except (URLError, TimeoutError, OSError):
                return False

        wait_for(frontend_ready)
        return api

    def live_sample(self, token):
        output = self.compose(
            "exec",
            "-T",
            "backend",
            "python",
            "-c",
            WS_PROBE,
            input_text=json.dumps({"token": token}),
        )
        try:
            sample = json.loads(output)
            require(sample["type"] == "sample.created", "WebSocket returned an unexpected event.")
            require(sample["device_id"] == "device-002", "WebSocket device filter did not match.")
            require(
                sample["is_anomaly"] == (Decimal(sample["value"]) > Decimal(sample["threshold"])),
                "WebSocket anomaly flag did not match.",
            )
            return sample
        except (ValueError, KeyError, TypeError) as exc:
            raise SmokeFailure("The WebSocket probe returned invalid data.") from exc

    def cleanup(self):
        if self.owns_resources:
            self.compose("down", "--volumes", "--remove-orphans")
            self.owns_resources = False
        if self.owns_env:
            self.env_file.unlink()
            self.owns_env = False


def persisted_sample(api, token, event_id):
    result = api.request(
        "GET", "/api/v1/monitoring/samples?" + urlencode({"event_id": event_id}), token=token
    )
    return result["items"][0] if result["total"] == 1 else None


def verify(deployment: Deployment, credentials):
    print("Starting fresh MariaDB, migration, bootstrap, API and Streamlit...", flush=True)
    api = deployment.start(build=True)
    spec = api.request("GET", "/openapi.json")
    require(
        "realtime analysis requires Admin" not in spec["info"]["description"],
        "Swagger contains outdated realtime permissions.",
    )
    require(
        "HTTPValidationError"
        not in json.dumps(spec["paths"]["/api/v1/records"]["post"]["responses"]["422"]),
        "Swagger still documents the default validation envelope.",
    )
    admin = api.login(credentials["BOOTSTRAP_ADMIN_EMAIL"], credentials["BOOTSTRAP_ADMIN_PASSWORD"])
    bootstrap = api.request("GET", "/api/v1/users/me", token=admin)
    require(bootstrap["role"] == "admin", "Fresh bootstrap account was not an administrator.")
    accounts = {}
    for role in ("admin", "user", "viewer"):
        email, password = (
            f"clean-{role}-{secrets.token_hex(5)}@example.com",
            secrets.token_urlsafe(24),
        )
        user = api.request(
            "POST",
            "/api/v1/auth/register",
            payload={"email": email, "password": password},
            expected=201,
        )
        if role != "user":
            api.request(
                "PATCH", f"/api/v1/admin/users/{user['id']}", token=admin, payload={"role": role}
            )
        accounts[role] = api.login(email, password)
    user, alternate_admin, viewer = accounts["user"], accounts["admin"], accounts["viewer"]
    record = api.request(
        "POST",
        "/api/v1/records",
        token=user,
        payload={
            "title": "Clean acceptance record",
            "value": "25.123456",
            "category": "device-002",
        },
        expected=201,
    )
    record_path = f"/api/v1/records/{record['id']}"
    api.request("PATCH", record_path, token=viewer, payload={"value": "1"}, expected=403)
    api.request("GET", "/api/v1/admin/users", token=user, expected=403)
    api.request("PATCH", record_path, token=alternate_admin, payload={"value": "42.654321"})
    saved_record = api.request("GET", record_path, token=user)
    require(
        Decimal(saved_record["value"]) == Decimal("42.654321"), "CRUD changed decimal precision."
    )
    page = api.request(
        "GET",
        "/api/v1/records?"
        + urlencode(
            {
                "q": "Clean acceptance record",
                "category": "device-002",
                "sort_by": "timestamp",
                "sort_order": "desc",
            }
        ),
        token=user,
    )
    require(
        page["total"] == 1 and page["items"][0]["id"] == record["id"],
        "Record filtering did not match.",
    )
    live = deployment.live_sample(user)
    saved_sample = wait_for(lambda: persisted_sample(api, user, live["event_id"]))
    print(
        "PASS: fresh bootstrap, Swagger, roles, CRUD, WebSocket and batch persistence.", flush=True
    )
    demoted = api.request(
        "PATCH",
        f"/api/v1/admin/users/{bootstrap['id']}",
        token=alternate_admin,
        payload={"role": "viewer"},
    )
    api.request("GET", "/api/v1/users/me", token=admin, expected=401)
    print("Recreating the complete stack with the same isolated database volume...", flush=True)
    deployment.compose("down", "--remove-orphans")  # Deliberately retains this project's volume.
    api = deployment.start(build=False)
    after_token = api.login(
        credentials["BOOTSTRAP_ADMIN_EMAIL"], credentials["BOOTSTRAP_ADMIN_PASSWORD"]
    )
    after = api.request("GET", "/api/v1/users/me", token=after_token)
    require(
        after == demoted and after["role"] == "viewer",
        "Bootstrap rerun changed the existing account.",
    )
    api.request("GET", "/api/v1/admin/users", token=after_token, expected=403)
    require(
        api.request("GET", record_path, token=user) == saved_record,
        "A record changed or disappeared after recreation.",
    )
    require(
        persisted_sample(api, user, live["event_id"]) == saved_sample,
        "A committed sample changed or disappeared after recreation.",
    )
    next_sample = deployment.live_sample(user)
    require(
        next_sample["event_id"] != live["event_id"],
        "The restarted simulator did not produce a new event.",
    )
    wait_for(lambda: persisted_sample(api, user, next_sample["event_id"]))
    api.request("DELETE", record_path, token=user, expected=204)
    api.request("GET", record_path, token=user, expected=404)
    print(
        "PASS: demoted bootstrap preserved; migration, login, records and live history "
        "survive recreation.",
        flush=True,
    )


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="datapulse-clean-" + secrets.token_hex(6))
    parser.add_argument("--backend-port", type=int, default=18010)
    parser.add_argument("--frontend-port", type=int, default=18510)
    options = parser.parse_args(argv)
    if not PROJECT_PATTERN.fullmatch(options.project):
        parser.error(
            "--project must start with datapulse-clean- and contain only "
            "lowercase letters, digits and hyphens"
        )
    return options


def main(argv=None):
    options = arguments(argv)
    deployment = Deployment(ROOT, options.project, options.backend_port, options.frontend_port)
    result = 0
    try:
        credentials = deployment.prepare()
        print(f"Isolated Compose project: {options.project}", flush=True)
        verify(deployment, credentials)
    except KeyboardInterrupt:
        print("FAIL: acceptance interrupted.", file=sys.stderr)
        result = 130
    except SmokeFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        result = 1
    except Exception:
        print("FAIL: unexpected acceptance failure; credentials were not printed.", file=sys.stderr)
        result = 1
    finally:
        try:
            deployment.cleanup()
        except Exception:
            print(
                f"FAIL: isolated cleanup did not finish; inspect {options.project}.",
                file=sys.stderr,
            )
            result = 1
    if result == 0:
        print("PASS: removed isolated containers, volume and generated credentials.", flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
