from __future__ import annotations

import shlex
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from ridge.backends._helper import HelperOperations, HelperTransportResult
from ridge.backends._source import HELPER_SOURCE, TRANSFER_HELPER_SOURCE
from ridge.backends._transfer import ProcessTransferOperations
from ridge.errors import InvalidPathError, PathTypeError, ResourceUnavailableError
from ridge.model import (
    ExecResult,
    FileStat,
    ListEntry,
    PropertyScalar,
    ResourceProperty,
    TransferPayloadKind,
)
from ridge.resource import ResourceCapabilities, TransferDestination, TransferSource


class SshResource:
    provider_name = "ssh"

    def __init__(
        self,
        name: str,
        *,
        host: str,
        root: str,
        python_executable: str,
        user: str | None = None,
        port: int | None = None,
        identity_file: Path | None = None,
        known_hosts_file: Path | None = None,
        ssh_executable: str = "ssh",
        configured_properties: Mapping[str, PropertyScalar] | None = None,
    ) -> None:
        if not host or host.startswith("-"):
            raise ValueError("host must be non-empty and must not begin with '-'")
        if not python_executable:
            raise ValueError("python executable must not be empty")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        root_path = PurePosixPath(root)
        if not root_path.is_absolute():
            raise InvalidPathError(f"SSH resource root must be absolute: {root}")
        self.name = name
        self.host = host
        self.root = root_path.as_posix()
        self.python_executable = python_executable
        self.user = user
        self.port = port
        self.identity_file = identity_file
        self.known_hosts_file = known_hosts_file
        self.ssh_executable = ssh_executable
        self._configured_properties = dict(configured_properties or {})
        self._operations = HelperOperations(self)
        self._transfer = ProcessTransferOperations(self.name, self._transfer_command)
        self.capabilities = ResourceCapabilities(
            compute=self,
            filesystem=self,
            transfer=self,
        )

    def _connection_arguments(self) -> tuple[str, ...]:
        arguments = [
            self.ssh_executable,
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "LogLevel=ERROR",
        ]
        if self.port is not None:
            arguments.extend(("-p", str(self.port)))
        if self.user is not None:
            arguments.extend(("-l", self.user))
        if self.identity_file is not None:
            arguments.extend(("-i", str(self.identity_file)))
        if self.known_hosts_file is not None:
            arguments.extend(("-o", f"UserKnownHostsFile={self.known_hosts_file}"))
        arguments.append(self.host)
        return tuple(arguments)

    def _transfer_command(self, operation: str) -> tuple[str, ...]:
        remote_command = shlex.join(
            (self.python_executable, "-c", TRANSFER_HELPER_SOURCE, self.root, operation)
        )
        return (*self._connection_arguments(), remote_command)

    def open_transfer_source(self, path: str) -> TransferSource:
        kind = self.stat(path).kind
        if kind not in ("file", "directory"):
            raise PathTypeError(f"copy source must be a regular file or directory: {path}")
        return self._transfer.open_source(path, "file" if kind == "file" else "tree")

    def open_transfer_destination(
        self, path: str, kind: TransferPayloadKind
    ) -> TransferDestination:
        return self._transfer.open_destination(path, kind)

    def _ssh(
        self,
        remote_command: str,
        *,
        input_bytes: bytes,
        timeout_seconds: float | None,
    ) -> subprocess.CompletedProcess[bytes]:
        command = (*self._connection_arguments(), remote_command)
        try:
            return subprocess.run(
                command,
                input=input_bytes,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise ResourceUnavailableError(
                f"SSH executable does not exist: {self.ssh_executable}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ResourceUnavailableError(f"SSH resource {self.name!r} did not respond") from exc
        except OSError as exc:
            raise ResourceUnavailableError(f"cannot invoke SSH: {exc}") from exc

    @staticmethod
    def _failure_message(completed: subprocess.CompletedProcess[bytes]) -> str:
        raw = completed.stderr or completed.stdout
        message = raw.decode(errors="replace").strip()
        return message or f"SSH exited with status {completed.returncode}"

    def invoke_helper(
        self,
        operation: str,
        request_bytes: bytes,
        *,
        timeout_seconds: float | None,
    ) -> HelperTransportResult:
        remote_command = shlex.join(
            (self.python_executable, "-c", HELPER_SOURCE, self.root, operation)
        )
        completed = self._ssh(
            remote_command,
            input_bytes=request_bytes,
            timeout_seconds=timeout_seconds,
        )
        if completed.returncode:
            raise ResourceUnavailableError(self._failure_message(completed))
        return HelperTransportResult(stdout=completed.stdout, stderr=completed.stderr)

    def inspect_properties(self) -> Mapping[str, ResourceProperty]:
        properties = {
            key: ResourceProperty(value=value, source="configured")
            for key, value in self._configured_properties.items()
        }
        configured: dict[str, PropertyScalar] = {
            "host": self.host,
            "root": self.root,
            "helper.python": self.python_executable,
            "ssh.executable": self.ssh_executable,
        }
        if self.user is not None:
            configured["user"] = self.user
        if self.port is not None:
            configured["port"] = self.port
        if self.identity_file is not None:
            configured["identity_file"] = str(self.identity_file)
        if self.known_hosts_file is not None:
            configured["known_hosts_file"] = str(self.known_hosts_file)
        for key, value in configured.items():
            properties[key] = ResourceProperty(value=value, source="configured")
        detected: dict[str, PropertyScalar] = {
            "location": "remote",
            **self._operations.probe(),
        }
        for key, value in detected.items():
            properties[key] = ResourceProperty(value=value, source="detected")
        return properties

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
