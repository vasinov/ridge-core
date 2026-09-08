from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import cast

from ridge.backends._helper import (
    HelperOperations,
    HelperTransportResult,
)
from ridge.backends._source import HELPER_SOURCE, TRANSFER_HELPER_SOURCE
from ridge.backends._transfer import ProcessTransferOperations
from ridge.errors import InvalidPathError, PathTypeError, ResourceUnavailableError
from ridge.model import (
    DeleteResult,
    ExecResult,
    FileStat,
    ListEntry,
    PropertyScalar,
    ResourceProperty,
    TransferPayloadKind,
)
from ridge.resource import ResourceCapabilities, TransferDestination, TransferSource


class DockerResource:
    provider_name = "docker"

    def __init__(
        self,
        name: str,
        *,
        container: str,
        root: str,
        python_executable: str,
        docker_executable: str = "docker",
        configured_properties: Mapping[str, PropertyScalar] | None = None,
    ) -> None:
        if not container:
            raise ValueError("container must not be empty")
        if not python_executable:
            raise ValueError("python executable must not be empty")
        root_path = PurePosixPath(root)
        if not root_path.is_absolute():
            raise InvalidPathError(f"Docker resource root must be absolute: {root}")
        self.name = name
        self.container = container
        self.root = root_path.as_posix()
        self.python_executable = python_executable
        self.docker_executable = docker_executable
        self._configured_properties = dict(configured_properties or {})
        self._operations = HelperOperations(self)
        self._transfer = ProcessTransferOperations(self.name, self._transfer_command)
        self.capabilities = ResourceCapabilities(
            compute=self,
            filesystem=self,
            transfer=self,
            delete=self,
        )

    def _transfer_command(self, operation: str) -> tuple[str, ...]:
        return (
            self.docker_executable,
            "exec",
            "--interactive",
            self.container,
            self.python_executable,
            "-c",
            TRANSFER_HELPER_SOURCE,
            self.root,
            operation,
        )

    def open_transfer_source(self, path: str) -> TransferSource:
        kind = self.stat(path).kind
        if kind not in ("file", "directory"):
            raise PathTypeError(f"copy source must be a regular file or directory: {path}")
        return self._transfer.open_source(path, "file" if kind == "file" else "tree")

    def open_transfer_destination(
        self, path: str, kind: TransferPayloadKind
    ) -> TransferDestination:
        return self._transfer.open_destination(path, kind)

    def _docker(
        self,
        arguments: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        timeout_seconds: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                (self.docker_executable, *arguments),
                input=input_bytes,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise ResourceUnavailableError(
                f"Docker executable does not exist: {self.docker_executable}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ResourceUnavailableError(
                f"Docker resource {self.name!r} did not respond"
            ) from exc
        except OSError as exc:
            raise ResourceUnavailableError(f"cannot invoke Docker: {exc}") from exc

    @staticmethod
    def _failure_message(completed: subprocess.CompletedProcess[bytes]) -> str:
        raw = completed.stderr or completed.stdout
        message = raw.decode(errors="replace").strip()
        return message or f"Docker exited with status {completed.returncode}"

    def _container_inspection(self) -> Mapping[str, object]:
        completed = self._docker(("container", "inspect", self.container), timeout_seconds=10)
        if completed.returncode:
            raise ResourceUnavailableError(self._failure_message(completed))
        try:
            documents = cast(list[object], json.loads(completed.stdout))
            if not documents:
                raise TypeError
            inspection_value = documents[0]
        except (json.JSONDecodeError, IndexError, TypeError) as exc:
            raise ResourceUnavailableError(
                "Docker returned an invalid container inspection"
            ) from exc
        if not isinstance(inspection_value, dict):
            raise ResourceUnavailableError("Docker returned an invalid container inspection")
        inspection = cast(dict[str, object], inspection_value)
        state_value = inspection.get("State")
        state = cast(dict[str, object], state_value) if isinstance(state_value, dict) else None
        if state is None or not state.get("Running"):
            status = state.get("Status", "unknown") if state is not None else "unknown"
            raise ResourceUnavailableError(
                f"Docker container {self.container!r} is not running (status: {status})"
            )
        return inspection

    def inspect_properties(self) -> Mapping[str, ResourceProperty]:
        inspection = self._container_inspection()
        config_value = inspection.get("Config")
        config = cast(dict[str, object], config_value) if isinstance(config_value, dict) else None
        image = config.get("Image") if config is not None else None
        properties = {
            key: ResourceProperty(value=value, source="configured")
            for key, value in self._configured_properties.items()
        }
        configured: dict[str, PropertyScalar] = {
            "container": self.container,
            "root": self.root,
            "helper.python": self.python_executable,
            "docker.executable": self.docker_executable,
        }
        for key, value in configured.items():
            properties[key] = ResourceProperty(value=value, source="configured")
        detected: dict[str, PropertyScalar] = {
            "location": "docker",
            "container_id": str(inspection.get("Id", "")),
            "image": str(image or ""),
            "status": "running",
        }
        for key, value in detected.items():
            properties[key] = ResourceProperty(value=value, source="detected")
        return properties

    def invoke_helper(
        self,
        operation: str,
        request_bytes: bytes,
        *,
        timeout_seconds: float | None,
    ) -> HelperTransportResult:
        completed = self._docker(
            (
                "exec",
                "--interactive",
                self.container,
                self.python_executable,
                "-c",
                HELPER_SOURCE,
                self.root,
                operation,
            ),
            input_bytes=request_bytes,
            timeout_seconds=timeout_seconds,
        )
        if completed.returncode:
            raise ResourceUnavailableError(self._failure_message(completed))
        return HelperTransportResult(stdout=completed.stdout, stderr=completed.stderr)

    def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecResult:
        return self._operations.exec(argv, cwd=cwd, env=env, timeout_seconds=timeout_seconds)

    def list(self, path: str = ".") -> tuple[ListEntry, ...]:
        return self._operations.list(path)

    def read(self, path: str, *, max_bytes: int | None = None) -> bytes:
        return self._operations.read(path, max_bytes=max_bytes)

    def write(
        self,
        path: str,
        content: bytes,
    ) -> None:
        self._operations.write(path, content)

    def stat(self, path: str) -> FileStat:
        return self._operations.stat(path)

    def delete(self, path: str, *, recursive: bool = False) -> DeleteResult:
        return self._operations.delete(path, recursive=recursive)
