from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import BinaryIO

from ridge.backends._scripts.deletion import DeletePathError, delete_path
from ridge.backends._source import TRANSFER_HELPER_SOURCE
from ridge.backends._transfer import ProcessTransferOperations
from ridge.errors import (
    ExecutionError,
    ExecutionTimeoutError,
    InvalidPathError,
    OutputLimitExceededError,
    PathNotFoundError,
    PathTypeError,
)
from ridge.model import (
    DeleteResult,
    ExecResult,
    FileKind,
    FileStat,
    ListEntry,
    PropertyScalar,
    ResourceProperty,
    TransferPayloadKind,
)
from ridge.resource import ResourceCapabilities, TransferDestination, TransferSource


class _RootedFilesystem:
    def __init__(self, root: Path) -> None:
        try:
            resolved_root = root.expanduser().resolve(strict=True)
        except FileNotFoundError as exc:
            raise PathNotFoundError(f"resource root does not exist: {root}") from exc
        except OSError as exc:
            raise InvalidPathError(f"cannot resolve resource root {root}: {exc}") from exc
        if not resolved_root.is_dir():
            raise PathTypeError(f"resource root is not a directory: {root}")
        self.root = resolved_root

    def resolve(self, path: str) -> Path:
        requested = Path(path)
        if requested.is_absolute():
            raise InvalidPathError(f"resource paths must be relative: {path}")
        resolved = (self.root / requested).resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise InvalidPathError(f"path escapes resource root: {path}")
        return resolved

    def display_path(self, path: Path) -> str:
        relative = path.relative_to(self.root)
        return "." if relative == Path(".") else relative.as_posix()

    @staticmethod
    def kind(path: Path) -> FileKind:
        if path.is_symlink():
            return "symlink"
        if path.is_file():
            return "file"
        if path.is_dir():
            return "directory"
        return "other"

    def list(self, path: str = ".") -> tuple[ListEntry, ...]:
        target = self.resolve(path)
        if not target.exists():
            raise PathNotFoundError(f"path does not exist: {path}")
        if not target.is_dir():
            raise PathTypeError(f"path is not a directory: {path}")
        entries: list[ListEntry] = []
        for child in sorted(target.iterdir(), key=lambda candidate: candidate.name):
            metadata = child.lstat()
            entries.append(
                ListEntry(
                    path=self.display_path(child),
                    kind=self.kind(child),
                    size=metadata.st_size,
                )
            )
        return tuple(entries)

    def read(self, path: str, *, max_bytes: int | None = None) -> bytes:
        target = self.resolve(path)
        if not target.exists():
            raise PathNotFoundError(f"path does not exist: {path}")
        if not target.is_file():
            raise PathTypeError(f"path is not a file: {path}")
        if max_bytes is not None:
            if max_bytes < 0:
                raise ValueError("max_bytes must be non-negative or None")
            size = target.stat().st_size
            if size > max_bytes:
                raise OutputLimitExceededError(
                    f"file is {size} bytes, exceeding the {max_bytes}-byte limit: {path}"
                )
        with target.open("rb") as stream:
            content = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
        if max_bytes is not None and len(content) > max_bytes:
            raise OutputLimitExceededError(f"file exceeds the {max_bytes}-byte limit: {path}")
        return content

    def write(
        self,
        path: str,
        content: bytes,
    ) -> None:
        requested = Path(path)
        if requested.is_absolute():
            raise InvalidPathError(f"resource paths must be relative: {path}")
        resolved = self.resolve(path)
        if resolved == self.root:
            raise PathTypeError(f"path is a directory: {path}")
        unresolved = self.root / requested
        try:
            unresolved.parent.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise PathTypeError(f"parent path is not a directory: {path}") from exc
        except OSError as exc:
            raise InvalidPathError(f"cannot create destination parents for {path}: {exc}") from exc
        parent = unresolved.parent.resolve(strict=True)
        if not parent.is_relative_to(self.root):
            raise InvalidPathError(f"path escapes resource root: {path}")
        target = parent / unresolved.name
        if not target.parent.is_dir():
            raise PathTypeError(f"parent path is not a directory: {path}")
        if target.exists() and target.is_dir() and not target.is_symlink():
            raise PathTypeError(f"path is a directory: {path}")
        if target.exists() and not (target.is_file() or target.is_symlink()):
            raise PathTypeError(f"path is not a regular file or symbolic link: {path}")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".ridge-write-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def stat(self, path: str) -> FileStat:
        target = self.resolve(path)
        if not target.exists() and not target.is_symlink():
            raise PathNotFoundError(f"path does not exist: {path}")
        metadata = target.lstat()
        return FileStat(
            path=self.display_path(target),
            kind=self.kind(target),
            size=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
        )


class _InspectableResource:
    provider_name: str

    def __init__(
        self,
        name: str,
        root: Path,
        configured_properties: Mapping[str, PropertyScalar] | None = None,
    ) -> None:
        self.name = name
        self._filesystem = _RootedFilesystem(root)
        self._configured_properties = dict(configured_properties or {})
        self._transfer = ProcessTransferOperations(self.name, self._transfer_command)
        self.capabilities = ResourceCapabilities(filesystem=self, transfer=self, delete=self)

    @property
    def root(self) -> Path:
        return self._filesystem.root

    def _transfer_command(self, operation: str) -> tuple[str, ...]:
        return (
            sys.executable,
            "-c",
            TRANSFER_HELPER_SOURCE,
            str(self.root),
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

    def _detected_properties(self) -> Mapping[str, PropertyScalar]:
        return {"location": "local"}

    def inspect_properties(self) -> Mapping[str, ResourceProperty]:
        properties = {
            key: ResourceProperty(value=value, source="configured")
            for key, value in self._configured_properties.items()
        }
        for key, value in self._detected_properties().items():
            properties[key] = ResourceProperty(value=value, source="detected")
        return properties

    def list(self, path: str = ".") -> tuple[ListEntry, ...]:
        return self._filesystem.list(path)

    def read(self, path: str, *, max_bytes: int | None = None) -> bytes:
        return self._filesystem.read(path, max_bytes=max_bytes)

    def write(
        self,
        path: str,
        content: bytes,
    ) -> None:
        self._filesystem.write(path, content)

    def stat(self, path: str) -> FileStat:
        return self._filesystem.stat(path)

    def delete(self, path: str, *, recursive: bool = False) -> DeleteResult:
        try:
            return DeleteResult(delete_path(self.root, path, recursive=recursive))
        except DeletePathError as exc:
            error = InvalidPathError if exc.kind == "invalid_path" else PathTypeError
            raise error(str(exc)) from exc
        except (OSError, RuntimeError) as exc:
            raise ExecutionError(f"cannot delete {path}: {exc}") from exc


class LocalResource(_InspectableResource):
    provider_name = "local"

    def __init__(
        self,
        name: str,
        root: Path,
        configured_properties: Mapping[str, PropertyScalar] | None = None,
    ) -> None:
        super().__init__(name, root, configured_properties)
        self.capabilities = ResourceCapabilities(
            compute=self,
            filesystem=self,
            transfer=self,
            delete=self,
        )

    def _detected_properties(self) -> Mapping[str, PropertyScalar]:
        return {
            "location": "local",
            "os": platform.system().lower(),
            "arch": platform.machine().lower(),
        }

    def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecResult:
        if not argv:
            raise ValueError("argv must contain at least one argument")
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative or None")
        working_directory = self.root if cwd is None else self._filesystem.resolve(cwd)
        if not working_directory.exists():
            raise PathNotFoundError(f"working directory does not exist: {cwd}")
        if not working_directory.is_dir():
            raise PathTypeError(f"working directory is not a directory: {cwd}")
        process_env = os.environ.copy()
        if env is not None:
            process_env.update(env)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                tuple(argv),
                cwd=working_directory,
                env=process_env,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExecutionTimeoutError(
                f"command exceeded its {timeout_seconds:g}-second timeout"
            ) from exc
        except OSError as exc:
            raise ExecutionError(f"cannot start command {argv[0]!r}: {exc}") from exc
        return ExecResult(
            argv=tuple(argv),
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started,
        )

    def exec_streaming(
        self,
        argv: Sequence[str],
        stdout: BinaryIO,
        stderr: BinaryIO,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecResult:
        if not argv:
            raise ValueError("argv must contain at least one argument")
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative or None")
        working_directory = self.root if cwd is None else self._filesystem.resolve(cwd)
        if not working_directory.exists():
            raise PathNotFoundError(f"working directory does not exist: {cwd}")
        if not working_directory.is_dir():
            raise PathTypeError(f"working directory is not a directory: {cwd}")
        process_env = os.environ.copy()
        if env is not None:
            process_env.update(env)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                tuple(argv),
                cwd=working_directory,
                env=process_env,
                stdout=stdout,
                stderr=stderr,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExecutionTimeoutError(
                f"command exceeded its {timeout_seconds:g}-second timeout"
            ) from exc
        except OSError as exc:
            raise ExecutionError(f"cannot start command {argv[0]!r}: {exc}") from exc
        return ExecResult(
            argv=tuple(argv),
            exit_code=completed.returncode,
            stdout=b"",
            stderr=b"",
            duration_seconds=time.monotonic() - started,
        )
