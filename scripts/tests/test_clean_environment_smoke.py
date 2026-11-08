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
