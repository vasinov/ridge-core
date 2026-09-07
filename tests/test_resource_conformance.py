import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

import pytest

from ridge.backends.docker import DockerResource
from ridge.backends.local import LocalResource
from ridge.backends.ssh import SshResource
from ridge.conformance import check_compute_capability, check_filesystem_capability
from ridge.model import ExecResult, FileStat, ListEntry


class ConformingResource(Protocol):
    def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecResult: ...

    def list(self, path: str = ".") -> tuple[ListEntry, ...]: ...

    def read(self, path: str, *, max_bytes: int | None = None) -> bytes: ...

    def write(
        self,
        path: str,
        content: bytes,
    ) -> None: ...

    def stat(self, path: str) -> FileStat: ...


class _LocalHelperDockerResource(DockerResource):
    """Exercise Docker's protocol adapter without requiring a daemon in unit tests."""

    def _docker(
        self,
        arguments: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        timeout_seconds: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        assert arguments[0] == "exec"
        return subprocess.run(
            [sys.executable, "-c", arguments[-3], arguments[-2], arguments[-1]],
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )


class _LocalHelperSshResource(SshResource):
    """Exercise SSH command quoting and the helper adapter without a network."""

    def _ssh(
        self,
        remote_command: str,
        *,
        input_bytes: bytes,
        timeout_seconds: float | None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["/bin/sh", "-c", remote_command],
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )


@pytest.fixture(params=["local", "docker", "ssh"])
def resource(request: pytest.FixtureRequest, tmp_path: Path) -> ConformingResource:
    if request.param == "local":
        return LocalResource("fixture", tmp_path)
    if request.param == "docker":
        return _LocalHelperDockerResource(
            "fixture",
            container="fixture",
            root=str(tmp_path),
            python_executable=sys.executable,
        )
    return _LocalHelperSshResource(
        "fixture",
        host="fixture",
        root=str(tmp_path),
        python_executable=sys.executable,
    )


def test_compute_contract_preserves_argv_cwd_and_output(
    resource: ConformingResource,
) -> None:
    resource.write("nested/input.txt", b"ridge")

    result = resource.exec(
        [
            sys.executable,
            "-c",
            "import pathlib,sys; print(pathlib.Path('input.txt').read_text(), sys.argv[1])",
            "a;b",
        ],
        cwd="nested",
    )

    assert result.exit_code == 0
    assert result.stdout == b"ridge a;b\n"
    assert result.stderr == b""


def test_filesystem_contract_round_trips_binary_data(resource: ConformingResource) -> None:
    resource.write("nested/data.bin", b"\x00ridge\xff")

    assert resource.read("nested/data.bin") == b"\x00ridge\xff"
    assert resource.stat("nested/data.bin").kind == "file"
    assert [entry.path for entry in resource.list("nested")] == ["nested/data.bin"]


def test_filesystem_write_replaces_by_default(resource: ConformingResource) -> None:
    resource.write("nested/result.txt", b"first")
    resource.write("nested/result.txt", b"second")

    assert resource.read("nested/result.txt") == b"second"


def test_reusable_compute_and_filesystem_conformance(resource: ConformingResource) -> None:
    check_filesystem_capability(resource)
    check_compute_capability(
        resource,
        [sys.executable, "-c", "print('ridge-conformance')"],
        expected_stdout=b"ridge-conformance\n",
    )
