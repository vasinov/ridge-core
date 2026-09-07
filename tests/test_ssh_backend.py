import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ridge.backends._helper import HELPER_SOURCE
from ridge.backends.ssh import SshResource
from ridge.errors import InvalidPathError, ResourceUnavailableError
from ridge.registry import ResourceRegistry


def _success_response(**values: object) -> bytes:
    return json.dumps({"ok": True, **values}).encode()


def test_ssh_root_must_be_absolute() -> None:
    with pytest.raises(InvalidPathError, match="must be absolute"):
        SshResource(
            "remote",
            host="example.test",
            root="workspace",
            python_executable="python3",
        )


def test_exec_uses_noninteractive_ssh_and_safely_quotes_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identity = tmp_path / "identity with spaces"
    known_hosts = tmp_path / "known hosts"
    calls: list[tuple[tuple[str, ...], float | None]] = []

    def fake_run(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs["timeout"]))
        response = _success_response(
            argv=["printf", "a;b"],
            exit_code=0,
            stdout="YTti",
            stderr="",
            duration_seconds=0.1,
        )
        return subprocess.CompletedProcess(command, 0, response, b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    resource = SshResource(
        "remote",
        host="host-alias",
        user="ridge",
        port=2222,
        root="/workspace with spaces",
        python_executable="/usr/bin/python3",
        identity_file=identity,
        known_hosts_file=known_hosts,
        ssh_executable="/usr/bin/ssh",
    )

    result = resource.exec(["printf", "a;b"], timeout_seconds=2)

    command, timeout = calls[0]
    assert command[:9] == (
        "/usr/bin/ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
    )
    assert "LogLevel=ERROR" in command
    assert ("-p", "2222") == command[command.index("-p") : command.index("-p") + 2]
    assert ("-l", "ridge") == command[command.index("-l") : command.index("-l") + 2]
    assert shlex.split(command[-1]) == [
        "/usr/bin/python3",
        "-c",
        HELPER_SOURCE,
        "/workspace with spaces",
        "exec",
    ]
    assert timeout == 7
    assert result.stdout == b"a;b"


def test_transport_failure_is_resource_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 255, b"", b"Permission denied (publickey).")

    monkeypatch.setattr(subprocess, "run", fake_run)
    resource = SshResource(
        "remote",
        host="example.test",
        root="/workspace",
        python_executable="python3",
    )

    with pytest.raises(ResourceUnavailableError, match="Permission denied"):
        ResourceRegistry([resource]).inspect("remote")


def test_inspect_combines_configured_and_remote_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _success_response(
        properties={
            "remote.os": "linux",
            "remote.arch": "aarch64",
            "remote.hostname": "fixture",
            "helper.python_version": "3.13.7",
        }
    )

    def fake_run(command: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, response, b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    resource = SshResource(
        "remote",
        host="example.test",
        user="ridge",
        root="/workspace",
        python_executable="python3",
        configured_properties={"purpose": "tests"},
    )

    properties = ResourceRegistry([resource]).inspect("remote").properties

    assert properties["host"].source == "configured"
    assert properties["user"].source == "configured"
    assert properties["purpose"].source == "configured"
    assert properties["remote.os"].source == "detected"
    assert properties["remote.hostname"].value == "fixture"
