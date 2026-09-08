"""Shared helper protocol for resources reached through command transports."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from ridge.errors import (
    DestinationExistsError,
    ExecutionError,
    ExecutionTimeoutError,
    InvalidPathError,
    OutputLimitExceededError,
    PathNotFoundError,
    PathTypeError,
    RidgeError,
)
from ridge.model import ExecResult, FileKind, FileStat, ListEntry, PropertyScalar

HELPER_SOURCE = r"""
import base64
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time


def reply(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")


def fail(kind, message):
    reply({"ok": False, "error": kind, "message": message})
    raise SystemExit(0)


def rooted(root_text, requested_text="."):
    requested = Path(requested_text)
    if requested.is_absolute():
        fail("invalid_path", "resource paths must be relative: " + requested_text)
    try:
        root = Path(root_text).resolve(strict=True)
    except FileNotFoundError:
        fail("path_not_found", "resource root does not exist: " + root_text)
    except OSError as error:
        fail("invalid_path", "cannot resolve resource root " + root_text + ": " + str(error))
    if not root.is_dir():
        fail("path_type", "resource root is not a directory: " + root_text)
    try:
        target = (root / requested).resolve(strict=False)
        target.relative_to(root)
    except (OSError, ValueError):
        fail("invalid_path", "path escapes resource root: " + requested_text)
    return root, target


def kind(path):
    if path.is_symlink():
        return "symlink"
    if path.is_file():
        return "file"
    if path.is_dir():
        return "directory"
    return "other"


request_line = sys.stdin.buffer.readline()
try:
    request = json.loads(request_line)
except (UnicodeDecodeError, json.JSONDecodeError) as error:
    fail("protocol", "invalid Ridge helper request: " + str(error))

operation = sys.argv[2]
root_text = sys.argv[1]
path_text = request.get("path", ".")
root, target = rooted(root_text, path_text)

if operation == "exec":
    argv = request["argv"]
    if not argv:
        fail("execution", "argv must contain at least one argument")
    if not target.exists():
        fail("path_not_found", "working directory does not exist: " + path_text)
    if not target.is_dir():
        fail("path_type", "working directory is not a directory: " + path_text)
    process_env = os.environ.copy()
    process_env.update(request.get("env") or {})
    timeout = request.get("timeout_seconds")
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=target,
            env=process_env,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        fail("execution_timeout", "command exceeded its " + str(timeout) + "-second timeout")
    except OSError as error:
        fail("execution", "cannot start command " + repr(argv[0]) + ": " + str(error))
    reply({
        "ok": True,
        "argv": argv,
        "exit_code": completed.returncode,
        "stdout": base64.b64encode(completed.stdout).decode("ascii"),
        "stderr": base64.b64encode(completed.stderr).decode("ascii"),
        "duration_seconds": time.monotonic() - started,
    })
elif operation == "list":
    if not target.exists():
        fail("path_not_found", "path does not exist: " + path_text)
    if not target.is_dir():
        fail("path_type", "path is not a directory: " + path_text)
    entries = []
    for child in sorted(target.iterdir(), key=lambda item: item.name):
        metadata = child.lstat()
        relative = child.relative_to(root).as_posix()
        entries.append({"path": relative, "kind": kind(child), "size": metadata.st_size})
    reply({"ok": True, "entries": entries})
elif operation == "read":
    if not target.exists():
        fail("path_not_found", "path does not exist: " + path_text)
    if not target.is_file():
        fail("path_type", "path is not a file: " + path_text)
    maximum = request.get("max_bytes")
    size = target.stat().st_size
    if maximum is not None and size > maximum:
        fail("output_limit", "file is " + str(size) + " bytes, exceeding the " + str(maximum) + "-byte limit: " + path_text)
    with target.open("rb") as stream:
        content = stream.read() if maximum is None else stream.read(maximum + 1)
    if maximum is not None and len(content) > maximum:
        fail("output_limit", "file exceeds the " + str(maximum) + "-byte limit: " + path_text)
    reply({"ok": True, "content": base64.b64encode(content).decode("ascii")})
elif operation == "write":
    content = sys.stdin.buffer.read()
    if target == root:
        fail("path_type", "path is a directory: " + path_text)
    unresolved = root / Path(path_text)
    try:
        unresolved.parent.mkdir(parents=True, exist_ok=True)
        parent = unresolved.parent.resolve(strict=True)
        parent.relative_to(root)
        target = parent / unresolved.name
    except (OSError, RuntimeError, ValueError) as error:
        fail("invalid_path", "cannot create destination parents for " + path_text + ": " + str(error))
    if target.exists() and target.is_dir() and not target.is_symlink():
        fail("path_type", "path is a directory: " + path_text)
    if target.exists() and not (target.is_file() or target.is_symlink()):
        fail("path_type", "path is not a regular file or symbolic link: " + path_text)
    descriptor = None
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".ridge-write-", dir=target.parent)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
        os.replace(temporary, target)
    except OSError as error:
        fail("execution", "cannot write " + path_text + ": " + str(error))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    reply({"ok": True})
elif operation == "stat":
    if not target.exists() and not target.is_symlink():
        fail("path_not_found", "path does not exist: " + path_text)
    metadata = target.lstat()
    relative = target.relative_to(root)
    display = "." if str(relative) == "." else relative.as_posix()
    reply({
        "ok": True,
        "stat": {
            "path": display,
            "kind": kind(target),
            "size": metadata.st_size,
            "modified_ns": metadata.st_mtime_ns,
        },
    })
elif operation == "probe":
    reply({
        "ok": True,
        "properties": {
            "remote.os": platform.system().lower(),
            "remote.arch": platform.machine().lower(),
            "remote.hostname": platform.node(),
            "helper.python_version": platform.python_version(),
        },
    })
else:
    fail("protocol", "unknown Ridge helper operation: " + operation)
"""


@dataclass(frozen=True, slots=True)
class HelperTransportResult:
    stdout: bytes
    stderr: bytes = b""


class HelperTransport(Protocol):
    def invoke_helper(
        self,
        operation: str,
        request_bytes: bytes,
        *,
        timeout_seconds: float | None,
    ) -> HelperTransportResult: ...


_HELPER_ERRORS: dict[str, type[RidgeError]] = {
    "destination_exists": DestinationExistsError,
    "execution": ExecutionError,
    "execution_timeout": ExecutionTimeoutError,
    "invalid_path": InvalidPathError,
    "output_limit": OutputLimitExceededError,
    "path_not_found": PathNotFoundError,
    "path_type": PathTypeError,
    "protocol": ExecutionError,
}


class HelperOperations:
    """Backend-neutral operations implemented by the ephemeral remote helper."""

    def __init__(self, transport: HelperTransport) -> None:
        self._transport = transport

    def _invoke(
        self,
        operation: str,
        request: Mapping[str, object],
        *,
        content: bytes = b"",
        timeout_seconds: float | None = 10,
    ) -> Mapping[str, object]:
        request_bytes = json.dumps(request, separators=(",", ":")).encode() + b"\n" + content
        result = self._transport.invoke_helper(
            operation,
            request_bytes,
            timeout_seconds=timeout_seconds,
        )
        try:
            response = cast(object, json.loads(result.stdout))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            detail = result.stderr.decode(errors="replace").strip()
            suffix = f": {detail}" if detail else ""
            raise ExecutionError(f"invalid response from Ridge helper{suffix}") from exc
        if not isinstance(response, dict) or "ok" not in response:
            raise ExecutionError("invalid response from Ridge helper")
        response = cast(dict[str, object], response)
        if not response["ok"]:
            error_name = response.get("error")
            message = response.get("message")
            error_type = _HELPER_ERRORS.get(str(error_name), ExecutionError)
            raise error_type(str(message or "helper operation failed"))
        return response

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
        response = self._invoke(
            "exec",
            {
                "argv": list(argv),
                "path": cwd or ".",
                "env": dict(env or {}),
                "timeout_seconds": timeout_seconds,
            },
            timeout_seconds=None if timeout_seconds is None else timeout_seconds + 5,
        )
        try:
            stdout = base64.b64decode(cast(str, response["stdout"]), validate=True)
            stderr = base64.b64decode(cast(str, response["stderr"]), validate=True)
            return ExecResult(
                argv=tuple(cast(list[str], response["argv"])),
                exit_code=cast(int, response["exit_code"]),
                stdout=stdout,
                stderr=stderr,
                duration_seconds=float(cast(float | int | str, response["duration_seconds"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutionError("invalid execution result from Ridge helper") from exc

    def list(self, path: str = ".") -> tuple[ListEntry, ...]:
        response = self._invoke("list", {"path": path})
        try:
            entries = cast(list[dict[str, object]], response["entries"])
            return tuple(
                ListEntry(
                    path=cast(str, entry["path"]),
                    kind=cast(FileKind, entry["kind"]),
                    size=cast(int, entry["size"]),
                )
                for entry in entries
            )
        except (KeyError, TypeError) as exc:
            raise ExecutionError("invalid list result from Ridge helper") from exc

    def read(self, path: str, *, max_bytes: int | None = None) -> bytes:
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must be non-negative or None")
        response = self._invoke("read", {"path": path, "max_bytes": max_bytes})
        try:
            return base64.b64decode(cast(str, response["content"]), validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutionError("invalid read result from Ridge helper") from exc

    def write(
        self,
        path: str,
        content: bytes,
    ) -> None:
        self._invoke("write", {"path": path}, content=content)

    def stat(self, path: str) -> FileStat:
        response = self._invoke("stat", {"path": path})
        try:
            stat = cast(dict[str, object], response["stat"])
            return FileStat(
                path=cast(str, stat["path"]),
                kind=cast(FileKind, stat["kind"]),
                size=cast(int, stat["size"]),
                modified_ns=cast(int, stat["modified_ns"]),
            )
        except (KeyError, TypeError) as exc:
            raise ExecutionError("invalid stat result from Ridge helper") from exc

    def probe(self) -> Mapping[str, PropertyScalar]:
        response = self._invoke("probe", {"path": "."})
        try:
            raw = cast(dict[str, object], response["properties"])
            properties: dict[str, PropertyScalar] = {}
            for key, value in raw.items():
                if not isinstance(value, str | int | float | bool | type(None)):
                    raise TypeError
                properties[key] = value
            return properties
        except (KeyError, TypeError) as exc:
            raise ExecutionError("invalid probe result from Ridge helper") from exc
