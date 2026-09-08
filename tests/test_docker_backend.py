import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from ridge.backends._source import HELPER_SOURCE
from ridge.backends.docker import DockerResource
from ridge.errors import ExecutionTimeoutError, InvalidPathError, ResourceUnavailableError
from ridge.registry import ResourceRegistry


def _run_helper(
    root: Path,
    operation: str,
    request: dict[str, Any],
    content: bytes = b"",
) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-c", HELPER_SOURCE, str(root), operation],
        input=json.dumps(request).encode() + b"\n" + content,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_docker_root_must_be_absolute() -> None:
    with pytest.raises(InvalidPathError, match="must be absolute"):
        DockerResource(
            "container",
            container="fixture",
            root="workspace",
            python_executable="python3",
        )


def test_helper_filesystem_round_trip_and_symlink_boundary(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    written = _run_helper(
        root,
        "write",
        {"path": "nested/data.bin"},
        b"\x00ridge\xff",
    )
    listing = _run_helper(root, "list", {"path": "nested"})
    read = _run_helper(root, "read", {"path": "nested/data.bin", "max_bytes": 20})
    escaped = _run_helper(root, "read", {"path": "escape/secret", "max_bytes": None})

    assert written == {"ok": True}
    assert listing["entries"] == [{"path": "nested/data.bin", "kind": "file", "size": 7}]
    assert read == {"ok": True, "content": "AHJpZGdl/w=="}
    assert escaped["ok"] is False
    assert escaped["error"] == "invalid_path"


def test_helper_timeout_stops_command_inside_resource(tmp_path: Path) -> None:
    marker = tmp_path / "continued"
    response = _run_helper(
        tmp_path,
        "exec",
        {
            "path": ".",
            "argv": [
                sys.executable,
                "-c",
                "import pathlib,time; time.sleep(.2); pathlib.Path('continued').touch()",
            ],
            "env": {},
            "timeout_seconds": 0.01,
        },
    )
    time.sleep(0.25)

    assert response["ok"] is False
    assert response["error"] == "execution_timeout"
    assert not marker.exists()


def test_resource_exec_uses_helper_and_decodes_result(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[str, ...], float | None]] = []

    def fake_run(
        command: tuple[str, ...],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs["timeout"]))
        response = {
            "ok": True,
            "argv": ["printf", "hello"],
            "exit_code": 0,
            "stdout": "aGVsbG8=",
            "stderr": "",
            "duration_seconds": 0.1,
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(response).encode(), b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    resource = DockerResource(
        "container",
        container="fixture",
        root="/workspace",
        python_executable="/usr/bin/python3",
        docker_executable="/opt/docker",
    )

    result = resource.exec(["printf", "hello"], timeout_seconds=2)

    assert result.stdout == b"hello"
    assert calls[0][0][:6] == (
        "/opt/docker",
        "exec",
        "--interactive",
        "fixture",
        "/usr/bin/python3",
        "-c",
    )
    assert calls[0][1] == 7


def test_resource_translates_helper_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    response = {
        "ok": False,
        "error": "execution_timeout",
        "message": "command exceeded its 1-second timeout",
    }

    def fake_run(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, json.dumps(response).encode(), b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    resource = DockerResource(
        "container",
        container="fixture",
        root="/workspace",
        python_executable="python3",
    )

    with pytest.raises(ExecutionTimeoutError, match="1-second"):
        resource.exec(["sleep", "20"], timeout_seconds=1)


def test_inspect_rejects_stopped_container(monkeypatch: pytest.MonkeyPatch) -> None:
    inspection = [{"State": {"Running": False, "Status": "exited"}}]

    def fake_run(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, json.dumps(inspection).encode(), b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    resource = DockerResource(
        "container",
        container="fixture",
        root="/workspace",
        python_executable="python3",
    )

    with pytest.raises(ResourceUnavailableError, match="not running"):
        ResourceRegistry([resource]).inspect("container")


def test_inspect_preserves_configured_and_detected_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspection = [
        {
            "Id": "abc123",
            "Config": {"Image": "python:3.13-alpine"},
            "State": {"Running": True, "Status": "running"},
        }
    ]

    def fake_run(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, json.dumps(inspection).encode(), b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    resource = DockerResource(
        "container",
        container="fixture",
        root="/workspace",
        python_executable="python3",
        configured_properties={"purpose": "tests"},
    )

    properties = ResourceRegistry([resource]).inspect("container").properties

    assert properties["container"].source == "configured"
    assert properties["root"].source == "configured"
    assert properties["helper.python"].source == "configured"
    assert properties["purpose"].source == "configured"
    assert properties["container_id"].source == "detected"
    assert properties["image"].source == "detected"
