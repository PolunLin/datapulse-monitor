import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

script_path = Path(__file__).resolve().parents[1] / "clean_environment_smoke.py"
spec = importlib.util.spec_from_file_location("clean_environment_smoke", script_path)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def deployment(tmp_path):
    return smoke.Deployment(tmp_path, "datapulse-clean-unit", 18010, 18510)


@pytest.mark.parametrize(
    "project",
    [
        "datapulse-monitor",
        "datapulse-monitor-test",
        "../bad",
        "datapulse-clean-",
        "datapulse-clean-ABC",
    ],
)
def test_project_names_cannot_target_normal_stack(project):
    with pytest.raises(SystemExit):
        smoke.arguments(["--project", project])


def test_existing_environment_is_refused_without_docker_or_deletion(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("PRIVATE=preserve")
    monkeypatch.setattr(
        smoke, "execute", lambda *_args, **_kwargs: pytest.fail("must not call Docker")
    )
    instance = deployment(tmp_path)
    with pytest.raises(smoke.SmokeFailure, match="clean checkout"):
        instance.prepare()
    instance.cleanup()
    assert env.read_text() == "PRIVATE=preserve"


def test_environment_symlink_is_refused(tmp_path):
    (tmp_path / ".env").symlink_to(tmp_path / "missing-target")
    with pytest.raises(smoke.SmokeFailure, match="clean checkout"):
        deployment(tmp_path).prepare()
    assert (tmp_path / ".env").is_symlink()


@pytest.mark.parametrize("collision_index", [0, 1, 2])
def test_existing_container_volume_or_network_is_never_owned(
    tmp_path, monkeypatch, collision_index
):
    commands = []

    def execute(args, **_kwargs):
        commands.append(args)
        return "existing-resource" if len(commands) - 1 == collision_index else ""

    monkeypatch.setattr(smoke, "execute", execute)
    monkeypatch.setattr(smoke, "ensure_ports_available", lambda _ports: None)
    instance = deployment(tmp_path)
    with pytest.raises(smoke.SmokeFailure, match="already has resources"):
        instance.prepare()
    instance.cleanup()
    assert not instance.owns_resources and not instance.owns_env
    assert len(commands) == collision_index + 1
    assert all("com.docker.compose.project=datapulse-clean-unit" in c[-1] for c in commands)


@pytest.mark.parametrize(
    "ports", [(8010, 18510), (18010, 8510), (80, 18510), (18010, 18010), (70000, 18510)]
)
def test_invalid_or_development_ports_are_refused(ports):
    with pytest.raises(smoke.SmokeFailure):
        smoke.ensure_ports_available(ports)


def test_subprocess_failure_does_not_expose_stderr_or_input(tmp_path, monkeypatch):
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2, stdout="private-token", stderr="private-password"
        ),
    )
    with pytest.raises(smoke.SmokeFailure) as error:
        smoke.execute(["command"], cwd=tmp_path, input_text="private-input")
    assert "private-" not in str(error.value)


def test_http_error_does_not_expose_response_or_credentials():
    import io

    client = smoke.API("http://test")
    client.opener = SimpleNamespace(
        open=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            HTTPError("http://test", 401, "private-password", {}, io.BytesIO(b"private-token"))
        )
    )
    with pytest.raises(smoke.SmokeFailure) as error:
        client.login("secret@example.com", "private-password")
    assert "private-" not in str(error.value)
    assert "401" in str(error.value)


def test_cleanup_only_removes_its_explicit_project_and_generated_environment(tmp_path, monkeypatch):
    instance = deployment(tmp_path)
    instance.env_file.write_text("private-test-only")
    instance.owns_resources = instance.owns_env = True
    commands = []
    monkeypatch.setattr(
        smoke, "execute", lambda args, **kwargs: commands.append((args, kwargs)) or ""
    )
    instance.cleanup()
    args, kwargs = commands[0]
    assert args[:5] == ["docker", "compose", "--project-name", "datapulse-clean-unit", "--env-file"]
    assert args[-3:] == ["down", "--volumes", "--remove-orphans"]
    assert kwargs["cwd"] == tmp_path
    assert not instance.env_file.exists()
    instance.cleanup()
    assert len(commands) == 1


def test_cleanup_failure_keeps_environment_for_owned_recovery(tmp_path, monkeypatch):
    instance = deployment(tmp_path)
    instance.env_file.write_text("private-test-only")
    instance.owns_resources = instance.owns_env = True
    monkeypatch.setattr(
        instance,
        "compose",
        lambda *_args: (_ for _ in ()).throw(smoke.SmokeFailure("cleanup failed")),
    )
    with pytest.raises(smoke.SmokeFailure):
        instance.cleanup()
    assert instance.env_file.exists() and instance.owns_resources


def test_ws_probe_uses_stdin_and_checks_flat_event_payload(tmp_path, monkeypatch):
    instance = deployment(tmp_path)
    commands = []
    event = {
        "type": "sample.created",
        "event_id": "id",
        "device_id": "device-002",
        "value": "81.123456",
        "threshold": "80",
        "is_anomaly": True,
    }

    def compose(*args, **kwargs):
        commands.append((args, kwargs))
        return json.dumps(event)

    monkeypatch.setattr(instance, "compose", compose)
    assert instance.live_sample("private-token") == event
    args, kwargs = commands[0]
    assert "private-token" not in repr(args)
    assert json.loads(kwargs["input_text"]) == {"token": "private-token"}


def test_prepare_overrides_inherited_compose_and_credentials(tmp_path, monkeypatch):
    instance = deployment(tmp_path)
    values = {
        "JWT_SECRET": "generated-jwt",
        "DB_PASSWORD": "generated-db",
        "DB_ROOT_PASSWORD": "generated-root",
        "BOOTSTRAP_ADMIN_PASSWORD": "generated-admin",
        "BACKEND_PORT": "8010",
        "FRONTEND_PORT": "8510",
    }
    monkeypatch.setenv("COMPOSE_FILE", "/private/development.yaml")
    monkeypatch.setenv("DB_PASSWORD", "inherited-private-password")
    monkeypatch.setattr(smoke, "ensure_ports_available", lambda _ports: None)
    monkeypatch.setattr(smoke, "ensure_unused_project", lambda *_args: None)

    def initialize(*_args, **_kwargs):
        instance.env_file.write_text("\n".join(f"{k}={v}" for k, v in values.items()))
        return ""

    monkeypatch.setattr(smoke, "execute", initialize)
    assert instance.prepare() == values
    assert "COMPOSE_FILE" not in instance.environment
    assert instance.environment["DB_PASSWORD"] == "generated-db"
    assert instance.environment["BACKEND_PORT"] == "18010"
    assert instance.sensitive_values == {
        "generated-jwt",
        "generated-db",
        "generated-root",
        "generated-admin",
    }
    assert instance.owns_env and not instance.owns_resources
    instance.cleanup()


def test_main_cleans_owned_resources_even_on_interrupted_verification(
    tmp_path, monkeypatch, capsys
):
    instance = deployment(tmp_path)
    calls = []
    monkeypatch.setattr(smoke, "Deployment", lambda *_args: instance)
    monkeypatch.setattr(instance, "prepare", lambda: {"secret": "private-password"})
    monkeypatch.setattr(instance, "cleanup", lambda: calls.append("cleanup"))
    monkeypatch.setattr(smoke, "verify", lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert smoke.main([]) == 130
    assert calls == ["cleanup"]
    assert "private-password" not in capsys.readouterr().err


def test_api_remembers_login_credentials_and_tokens_for_diagnostic_redaction(monkeypatch):
    values = set()
    client = smoke.API("http://test", sensitive_values=values)
    monkeypatch.setattr(
        client, "request", lambda *_args, **_kwargs: {"access_token": "private-token"}
    )
    assert client.login("private@example.com", "private-password") == "private-token"
    assert values == {"private@example.com", "private-password", "private-token"}


def test_diagnostic_logs_keep_operational_events_without_credentials_bodies_or_sql():
    private_values = {"private-jwt", "private-password", "private-token", "private@example.com"}
    logs = "\n".join(
        [
            json.dumps(
                {
                    "message": "Database operation failed",
                    "level": "ERROR",
                    "error_type": "OperationalError",
                    "request_body": {"title": "private-body", "password": "private-password"},
                    "sql": "SELECT secret FROM users",
                    "parameters": {"email": "private@example.com"},
                    "authorization": "Bearer private-token",
                    "jwt_secret": "private-jwt",
                    "path": "/private-path?token=private-token",
                    "status_code": 503,
                }
            ),
            "OperationalError: bound SQL parameter private-sql-value",
            "mariadb | Access denied for user private@example.com using private-password",
            "request body private-unlabelled-value",
            "JWT_SECRET=private-jwt",
        ]
    )
    result = smoke.diagnostic_logs(logs, private_values)
    events = [json.loads(line) for line in result.splitlines()]
    assert events[0] == {
        "event": "Database operation failed",
        "level": "ERROR",
        "error_type": "OperationalError",
        "status_code": 503,
    }
    assert events[1] == {"error_type": "OperationalError"}
    assert events[2] == {"event": "Database authentication denied"}
    assert events[-1] == {"omitted_unstructured_lines": 2}
    assert "private-" not in result
    assert "SELECT" not in result and "parameters" not in result


def test_diagnostics_replace_registered_sensitive_values_even_in_allowed_events():
    output = smoke.diagnostic_logs("Application resources initialized", {"resources", ""})
    assert "resources" not in output
    assert "[REDACTED]" in output


@pytest.mark.parametrize(
    "line,event",
    [
        ("Starting MariaDB 11.7.2 source revision private-revision", "MariaDB server starting"),
        ("Initializing database files at private-path", "Database initialization starting"),
        ("Database files initialized at private-path", "Database initialization complete"),
        ("MariaDB init process done. Ready for start up.", "Database bootstrap complete"),
        (
            "INFO [alembic.runtime.migration] Context impl MySQLImpl.",
            "Alembic MySQL migration context initialized",
        ),
        (
            "INFO [alembic.runtime.migration] Running upgrade private-old -> private-new",
            "Database migration running",
        ),
    ],
)
def test_startup_logs_retain_canonical_database_and_migration_events(line, event):
    result = smoke.diagnostic_logs(line, set())
    assert json.loads(result.splitlines()[0]) == {"event": event}
    assert "private-" not in result


def test_main_collects_owned_service_diagnostics_before_cleanup(tmp_path, monkeypatch):
    instance = deployment(tmp_path)
    directory = tmp_path / "artifacts"
    calls = []
    monkeypatch.setattr(smoke, "Deployment", lambda *_args: instance)

    def prepare():
        instance.owns_env = instance.owns_resources = True
        return {}

    def compose(*args, **_kwargs):
        calls.append(args)
        if args[0] == "ps":
            return json.dumps(
                [
                    {
                        "Service": "migrate",
                        "State": "exited",
                        "Health": "",
                        "ExitCode": 1,
                        "Command": "password=private-command",
                        "Labels": "private-environment",
                    }
                ]
            )
        return "OperationalError: private-sql-parameter"

    monkeypatch.setattr(instance, "prepare", prepare)
    monkeypatch.setattr(instance, "compose", compose)
    monkeypatch.setattr(instance, "cleanup", lambda: calls.append(("cleanup",)))
    monkeypatch.setattr(
        smoke, "verify", lambda *_args: (_ for _ in ()).throw(smoke.SmokeFailure("failed"))
    )
    assert smoke.main(["--diagnostics-dir", str(directory)]) == 1
    assert calls[0] == ("ps", "--all", "--format", "json")
    assert calls[-1] == ("cleanup",)
    assert len(calls) == 6
    assert json.loads((directory / "service-status.json").read_text()) == [
        {
            "service": "migrate",
            "state": "exited",
            "health": "",
            "exit_code": 1,
        }
    ]
    assert all((directory / f"{service}.log").exists() for service in smoke.DIAGNOSTIC_SERVICES)
    assert "private-" not in "".join(path.read_text() for path in directory.iterdir())


def test_diagnostic_collection_continues_when_a_service_query_fails(tmp_path, monkeypatch):
    instance = deployment(tmp_path)
    instance.owns_env = instance.owns_resources = True
    directory = tmp_path / "artifacts"

    def compose(*args, **_kwargs):
        if args[0] == "ps" or args[-1] == "db":
            raise smoke.SmokeFailure("private-exception")
        return "Application startup complete."

    monkeypatch.setattr(instance, "compose", compose)
    instance.collect_diagnostics(directory)
    summary = (directory / "collection.txt").read_text()
    assert "Service status unavailable" in summary
    assert "db log summary unavailable" in summary
    assert (directory / "backend.log").exists()
    assert "private-exception" not in "".join(path.read_text() for path in directory.iterdir())


def test_diagnostic_commands_use_short_timeout_without_changing_other_compose_calls(
    tmp_path, monkeypatch
):
    instance = deployment(tmp_path)
    instance.env_file.write_text("private-owned-environment")
    instance.owns_env = instance.owns_resources = True
    commands = []

    def execute(args, **kwargs):
        commands.append((args, kwargs))
        return "[]" if "ps" in args else ""

    monkeypatch.setattr(smoke, "execute", execute)
    instance.collect_diagnostics(tmp_path / "artifacts")
    instance.compose("up", "-d")
    instance.cleanup()
    assert len(commands) == 7
    assert [kwargs["timeout"] for _, kwargs in commands] == [10] * 5 + [600, 600]
    assert commands[-2][0][-2:] == ["up", "-d"]
    assert commands[-1][0][-3:] == ["down", "--volumes", "--remove-orphans"]


def test_diagnostic_collection_failure_does_not_prevent_cleanup(tmp_path, monkeypatch, capsys):
    instance = deployment(tmp_path)
    calls = []
    monkeypatch.setattr(smoke, "Deployment", lambda *_args: instance)
    monkeypatch.setattr(instance, "prepare", dict)
    monkeypatch.setattr(
        smoke, "verify", lambda *_args: (_ for _ in ()).throw(smoke.SmokeFailure("failed"))
    )
    monkeypatch.setattr(
        instance,
        "collect_diagnostics",
        lambda *_args: (_ for _ in ()).throw(OSError("private-directory")),
    )
    monkeypatch.setattr(instance, "cleanup", lambda: calls.append("cleanup"))
    assert smoke.main(["--diagnostics-dir", str(tmp_path / "artifacts")]) == 1
    assert calls == ["cleanup"]
    assert "private-directory" not in capsys.readouterr().err


def test_existing_deployment_is_never_read_for_diagnostics(tmp_path, monkeypatch):
    instance = deployment(tmp_path)
    instance.env_file.write_text("PRIVATE=preserve")
    directory = tmp_path / "artifacts"
    monkeypatch.setattr(smoke, "Deployment", lambda *_args: instance)
    monkeypatch.setattr(
        smoke, "execute", lambda *_args, **_kwargs: pytest.fail("must not inspect Docker")
    )
    monkeypatch.setattr(
        smoke, "read_generated_environment", lambda *_args: pytest.fail("must not read .env")
    )
    assert smoke.main(["--diagnostics-dir", str(directory)]) == 1
    assert not directory.exists()
    assert instance.env_file.read_text() == "PRIVATE=preserve"


@pytest.mark.parametrize("owns_env,owns_resources", [(False, False), (True, False), (False, True)])
def test_diagnostics_require_both_environment_and_resource_ownership(
    tmp_path, monkeypatch, owns_env, owns_resources
):
    instance = deployment(tmp_path)
    instance.owns_env, instance.owns_resources = owns_env, owns_resources
    monkeypatch.setattr(
        instance, "compose", lambda *_args: pytest.fail("must not inspect unowned resources")
    )
    directory = tmp_path / "artifacts"
    instance.collect_diagnostics(directory)
    assert not directory.exists()
