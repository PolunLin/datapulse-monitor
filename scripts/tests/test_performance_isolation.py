"""Disposable performance runs cannot select or clean the development stack."""

import importlib.util
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

script_path = Path(__file__).resolve().parents[1] / "run_performance.py"
spec = importlib.util.spec_from_file_location("run_performance", script_path)
performance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(performance)


def private_run(directory, *, project="datapulse-perf-unit", environment=None):
    directory.mkdir(exist_ok=True)
    private = {
        "project": project,
        "port": 19110,
        "environment": environment or {"PERF_DB_PASSWORD": "generated-private-password"},
    }
    (directory / "private.json").write_text(json.dumps(private))
    (directory / "private.env").write_text("PERF_DB_PASSWORD=generated-private-password\n")
    return directory


@pytest.mark.parametrize(
    "project",
    [
        "datapulse-monitor",
        "datapulse-monitor-test",
        "other-perf-unit",
        "datapulse-perf-",
        "datapulse-perf-ABC",
        "datapulse-perf-équipement",
        "datapulse-perf-../development",
        "datapulse-perf-unit\n--project-name=datapulse-monitor",
    ],
)
def test_stack_rejects_invalid_or_development_project_before_docker(tmp_path, monkeypatch, project):
    private_run(tmp_path, project=project)
    monkeypatch.setattr(
        performance.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Invalid project must never reach Docker"),
    )
    with pytest.raises(ValueError, match="disposable performance namespace"):
        performance.Stack(tmp_path)


def test_compose_always_uses_explicit_private_project_file_and_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "datapulse-monitor")
    monkeypatch.setenv("COMPOSE_FILE", "/private/development-compose.yaml")
    monkeypatch.setenv("PERF_DB_PASSWORD", "inherited-development-password")
    stack = performance.Stack(private_run(tmp_path))
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="owned-result", stderr="")

    monkeypatch.setattr(performance.subprocess, "run", run)
    assert stack.compose("down", "--volumes", "--remove-orphans") == "owned-result"
    command, options = calls[0]
    assert command == [
        "docker",
        "compose",
        "--project-name",
        "datapulse-perf-unit",
        "--env-file",
        str(tmp_path / "private.env"),
        "-f",
        str(performance.ROOT / "compose.performance.yaml"),
        "down",
        "--volumes",
        "--remove-orphans",
    ]
    assert options["env"]["PERF_DB_PASSWORD"] == "generated-private-password"
    assert options["capture_output"] is True
    assert options["check"] is False
    assert options["timeout"] == 180
    assert "inherited-development-password" not in repr(command)
    assert "generated-private-password" not in repr(command)


def test_compose_failure_does_not_include_diagnostic_bodies(tmp_path, monkeypatch, capsys):
    stack = performance.Stack(private_run(tmp_path))
    monkeypatch.setattr(
        performance.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=12,
            stdout="private-token-in-stdout",
            stderr="private-password-in-stderr",
        ),
    )
    with pytest.raises(RuntimeError, match="Compose up exited with 12") as failure:
        stack.compose("up", "-d")
    assert "private-" not in str(failure.value)
    output = capsys.readouterr()
    assert "private-" not in output.out + output.err


@pytest.mark.parametrize("port", [8010, 8510, 0, 1023, 65536])
def test_setup_rejects_development_or_invalid_ports_before_docker(tmp_path, monkeypatch, port):
    directory = tmp_path / "new-run"
    monkeypatch.setattr(
        performance.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Invalid port must never reach Docker"),
    )
    with pytest.raises(ValueError, match="non-development port"):
        performance.setup(directory, port)
    assert not directory.exists()


def test_setup_preserves_existing_run_directory_without_docker(tmp_path, monkeypatch):
    marker = tmp_path / "private.env"
    marker.write_text("PRIVATE=preserve")
    monkeypatch.setattr(
        performance.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Existing directory must never reach Docker"),
    )
    with pytest.raises(FileExistsError):
        performance.setup(tmp_path, 19110)
    assert marker.read_text() == "PRIVATE=preserve"


@pytest.mark.parametrize("collision_index", [0, 1, 2])
def test_setup_refuses_preexisting_project_resources_before_writing_credentials(
    tmp_path, monkeypatch, collision_index
):
    directory = tmp_path / "new-run"
    calls = []
    sock = MagicMock()
    monkeypatch.setattr(performance.socket, "socket", lambda: sock)
    monkeypatch.setattr(performance.secrets, "token_hex", lambda _size: "abcd")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        existing = "preexisting-resource\n" if len(calls) - 1 == collision_index else ""
        return SimpleNamespace(returncode=0, stdout=existing, stderr="")

    monkeypatch.setattr(performance.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="existing Compose project"):
        performance.setup(directory, 19110)
    assert len(calls) == collision_index + 1
    assert [command[1] for command, _ in calls] == ["ps", "volume", "network"][
        : collision_index + 1
    ]
    for command, options in calls:
        assert command[-2] == "--filter"
        assert command[-1].startswith("label=com.docker.compose.project=datapulse-perf-")
        assert command[-1].endswith("-abcd")
        assert options["check"] is True
    assert list(directory.iterdir()) == []
    sock.__enter__.return_value.bind.assert_called_once_with(("127.0.0.1", 19110))


@pytest.mark.parametrize("existing_temporary", [False, True])
def test_private_json_writes_are_owner_only_even_when_replacing_existing_files(
    tmp_path, existing_temporary
):
    destination = tmp_path / "private.json"
    destination.write_text('{"secret": "old"}')
    destination.chmod(0o644)
    temporary = tmp_path / "private.json.tmp"
    if existing_temporary:
        temporary.write_text("stale temporary content")
        temporary.chmod(0o644)
    performance.write_json(destination, {"secret": "new-private-value"}, private=True)
    assert json.loads(destination.read_text()) == {"secret": "new-private-value"}
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not temporary.exists()


def test_private_json_refuses_temporary_symlink_and_preserves_target(tmp_path):
    destination = tmp_path / "private.json"
    target = tmp_path / "unrelated-file"
    target.write_text("preserve-unrelated-content")
    temporary = tmp_path / "private.json.tmp"
    temporary.symlink_to(target)
    with pytest.raises(OSError):
        performance.write_json(destination, {"secret": "new-private-value"}, private=True)
    assert target.read_text() == "preserve-unrelated-content"
    assert not destination.exists()


def test_successful_setup_writes_only_private_credentials_and_uses_dedicated_port(
    tmp_path, monkeypatch
):
    directory = tmp_path / "new-run"
    commands = []
    compose_calls = []
    monkeypatch.setattr(performance.socket, "socket", MagicMock())

    def run(command, **kwargs):
        commands.append(command)
        output = "4 8589934592 aarch64" if command[1] == "info" else ""
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(performance.subprocess, "run", run)
    monkeypatch.setattr(
        performance.Stack,
        "compose",
        lambda self, *args, **kwargs: compose_calls.append((self.project, args, kwargs)),
    )
    performance.setup(directory, 19110)
    for name in ("private.json", "private.env", "credentials.json"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    private = json.loads((directory / "private.json").read_text())
    credentials = json.loads((directory / "credentials.json").read_text())
    metadata = json.loads((directory / "metadata.json").read_text())
    assert private["port"] == 19110
    assert private["environment"]["PERF_BACKEND_PORT"] == "19110"
    assert credentials["password"] == private["environment"]["PERF_ADMIN_PASSWORD"]
    assert metadata["ws_max_connections"] == 0
    assert metadata["docker_host"]["architecture"] == "aarch64"
    assert [command[1] for command in commands] == ["ps", "volume", "network", "info"]
    assert [args[0] for _, args, _ in compose_calls] == ["build", "up"]
    assert all(project == private["project"] for project, _, _ in compose_calls)
