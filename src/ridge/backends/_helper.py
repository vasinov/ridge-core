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
