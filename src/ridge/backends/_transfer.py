"""Shared process transport for streamed cross-resource transfers."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from typing import BinaryIO, cast

from ridge._job_process import in_job_worker
from ridge.errors import (
    DestinationExistsError,
    InvalidPathError,
    PathNotFoundError,
    PathTypeError,
    ResourceUnavailableError,
    RidgeError,
    SourceChangedError,
    TransferError,
    format_error,
)
from ridge.model import TransferPayloadKind
from ridge.resource import TransferDestination, TransferSource

_TRANSFER_ERRORS: Mapping[str, type[RidgeError]] = {
    "destination_exists": DestinationExistsError,
    "invalid_path": InvalidPathError,
    "path_not_found": PathNotFoundError,
    "path_type": PathTypeError,
    "source_changed": SourceChangedError,
    "transfer": TransferError,
}


def _request_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


def _failure(resource_name: str, return_code: int, stderr: bytes) -> RidgeError:
    detail = stderr.decode(errors="replace").strip()
    try:
        value = cast(object, json.loads(detail))
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        failure = cast(dict[str, object], value)
        error = _TRANSFER_ERRORS.get(str(failure.get("error")))
        if error is not None:
            return error(
                f"resource {resource_name!r}: {failure.get('message') or 'transfer failed'}"
            )
    message = detail or f"transfer process exited with status {return_code}"
    return ResourceUnavailableError(f"resource {resource_name!r}: {message}")


class _Process:
    def __init__(self, process: subprocess.Popen[bytes], resource_name: str) -> None:
        self.process = process
        self.resource_name = resource_name

    def _wait(self, stderr: bytes) -> None:
        return_code = self.process.wait()
        if return_code:
            raise _failure(self.resource_name, return_code, stderr)

    def _stop(self) -> None:
        if self.process.poll() is not None:
            return
        with suppress(OSError):
            self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            with suppress(OSError):
                self.process.kill()
            self.process.wait()


class ProcessTransferSource(_Process, TransferSource):
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        resource_name: str,
        kind: TransferPayloadKind,
    ) -> None:
        super().__init__(process, resource_name)
        self.kind = kind
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise AssertionError("transfer source requires piped standard streams")
        self._stdout = cast(BinaryIO, process.stdout)
        self._stderr = cast(BinaryIO, process.stderr)

    def read(self, size: int) -> bytes:
        return self._stdout.read(size)

    def finish(self) -> None:
        stderr = self._stderr.read()
        self._wait(stderr)

    def cancel(self) -> None:
        with suppress(OSError):
            self._stdout.close()
        self._stop()
        with suppress(OSError):
            self._stderr.close()


class ProcessTransferDestination(_Process, TransferDestination):
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        resource_name: str,
        invoke_control: Callable[[str, Mapping[str, object]], None],
    ) -> None:
        super().__init__(process, resource_name)
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise AssertionError("transfer destination requires piped standard streams")
        self._stdin: BinaryIO | None = cast(BinaryIO, process.stdin)
        self._stdout = cast(BinaryIO, process.stdout)
        self._stderr = cast(BinaryIO, process.stderr)
        self._invoke_control = invoke_control
        self._token: str | None = None
        self._staging_stopped = False
        self._finished = False
        self._collected = False
        self._cancelled = False
        self._commit_attempted = False
        self._commit_uncertain = False

    @staticmethod
    def _response(content: bytes) -> dict[str, object]:
        try:
            response = cast(object, json.loads(content))
            if isinstance(response, dict):
                return cast(dict[str, object], response)
        except (UnicodeError, json.JSONDecodeError):
            pass
        raise TransferError("invalid response from transfer destination")

    def start(self) -> None:
        line = self._stdout.readline(64 * 1024)
        if not line:
            _, stderr = self.process.communicate(timeout=2)
            self._collected = True
            self._stdin = None
            self._wait(stderr)
        payload = self._response(line)
        token = payload.get("token")
        if (
            payload.get("ok") is not True
            or payload.get("ready") is not True
            or not isinstance(token, str)
            or not token
        ):
            raise TransferError("invalid staging readiness acknowledgement")
        self._token = token

    def write(self, content: bytes) -> None:
        if self._stdin is None:
            raise TransferError("transfer destination is already closed")
        try:
            remaining = memoryview(content)
            while remaining:
                written = self._stdin.write(remaining)
                if not written:
                    raise TransferError("transfer destination stopped accepting content")
                remaining = remaining[written:]
        except BrokenPipeError as exc:
            raise TransferError("transfer destination stopped accepting content") from exc

    def finish(self) -> tuple[int, int]:
        if self._collected:
            raise TransferError("transfer destination is already closed")
        self._stdin = None
        stdout, stderr = self.process.communicate()
        self._collected = True
        return self._completion(stdout, stderr)

    def _completion(self, stdout: bytes, stderr: bytes) -> tuple[int, int]:
        try:
            payload = self._response(stdout)
            if (
                self._token is None
                or payload.get("token") != self._token
                or payload.get("stopped") is not True
            ):
                raise TransferError("invalid staging completion acknowledgement")
        except TransferError as acknowledgement_error:
            if self.process.returncode:
                error = _failure(self.resource_name, self.process.returncode, stderr)
                error.add_note(str(acknowledgement_error))
                raise error from acknowledgement_error
            raise
        try:
            self._staging_stopped = True
            self._wait(stderr)
            if payload.get("ok") is not True:
                raise TypeError
            bytes_copied = payload["bytes_copied"]
            entries_copied = payload["entries_copied"]
            if (
                type(bytes_copied) is not int
                or bytes_copied < 0
                or type(entries_copied) is not int
                or entries_copied < 0
            ):
                raise TypeError
        except (KeyError, TypeError) as exc:
            raise TransferError("invalid response from transfer destination") from exc
        self._finished = True
        return bytes_copied, entries_copied

    def commit(self) -> None:
        if self._commit_attempted:
            raise TransferError("publication cannot be retried")
        if self._token is None or not self._finished or self._cancelled:
            raise TransferError("transfer destination has not finished staging")
        self._commit_attempted = True
        try:
            self._invoke_control("commit", {"token": self._token})
        except BaseException as error:
            # A helper-reported failure has completed. A broken transport or
            # interrupted caller cannot establish whether publication is ongoing.
            self._commit_uncertain = isinstance(error, ResourceUnavailableError) or not isinstance(
                error, RidgeError
            )
            if self._commit_uncertain:
                error.add_note(
                    f"resource {self.resource_name!r}: publication outcome unconfirmed; "
                    f"inspect destination and any remaining staging at {self._token}; "
                    "automatic cleanup was not attempted"
                )
            raise
        self._token = None

    def abort(self) -> None:
        if self._token is None:
            return
        if not self._staging_stopped:
            raise TransferError(
                f"resource {self.resource_name!r}: cleanup refused after unconfirmed staging; "
                f"inspect any remaining staging at {self._token}"
            )
        if self._commit_uncertain:
            raise TransferError(
                f"resource {self.resource_name!r}: cleanup refused after unconfirmed publication; "
                f"inspect any remaining staging at {self._token}"
            )
        token = self._token
        try:
            self._invoke_control("abort", {"token": token})
        except BaseException as error:
            error.add_note(
                f"resource {self.resource_name!r}: inspect any remaining staging at {token}"
            )
            raise
        self._token = None

    def cancel(self) -> None:
        self._cancelled = True
        if self._collected:
            return
        self._stdin = None
        try:
            # Unbuffered input closes without flushing a blocked payload write.
            # communicate drains both report pipes within the grace period.
            stdout, stderr = self.process.communicate(timeout=2)
            self._collected = True
            self._completion(stdout, stderr)
        except subprocess.TimeoutExpired as error:
            raise TransferError(
                f"resource {self.resource_name!r}: staging stop unconfirmed after cancellation; "
                f"inspect any remaining staging at {self._token or 'unknown path'}"
            ) from error
        finally:
            self._collected = True
            self._stop()
            for stream in (self.process.stdin, self._stdout, self._stderr):
                if stream is not None:
                    with suppress(OSError):
                        stream.close()


class ProcessTransferOperations:
    def __init__(
        self,
        resource_name: str,
        command: Callable[[str], Sequence[str]],
    ) -> None:
        self._resource_name = resource_name
        self._command = command

    def _start(self, operation: str) -> subprocess.Popen[bytes]:
        try:
            return subprocess.Popen(
                tuple(self._command(operation)),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0 if operation.startswith("stage-") else -1,
                # The coordinator must receive Ctrl-C so it can close both endpoints.
                start_new_session=not in_job_worker.get(),
            )
        except OSError as exc:
            raise ResourceUnavailableError(
                f"cannot start transfer for resource {self._resource_name!r}: {exc}"
            ) from exc

    def _invoke_control(self, operation: str, request: Mapping[str, object]) -> None:
        try:
            completed = subprocess.run(
                tuple(self._command(operation)),
                input=_request_bytes(request),
                capture_output=True,
                check=False,
                start_new_session=not in_job_worker.get(),
            )
        except OSError as exc:
            raise ResourceUnavailableError(
                f"cannot continue transfer for resource {self._resource_name!r}: {exc}"
            ) from exc
        if completed.returncode:
            raise _failure(self._resource_name, completed.returncode, completed.stderr)
        try:
            response = cast(object, json.loads(completed.stdout))
        except (UnicodeError, json.JSONDecodeError):
            response = None
        if (
            not isinstance(response, dict)
            or cast(dict[str, object], response).get("ok") is not True
        ):
            raise ResourceUnavailableError(
                f"resource {self._resource_name!r}: invalid {operation} acknowledgement"
            )

    def open_source(self, path: str, kind: TransferPayloadKind) -> ProcessTransferSource:
        process = self._start("export-file" if kind == "file" else "export-tree")
        assert process.stdin is not None
        try:
            process.stdin.write(_request_bytes({"path": path}))
            process.stdin.close()
        except OSError as exc:
            process.kill()
            process.wait()
            raise ResourceUnavailableError(
                f"cannot start transfer from resource {self._resource_name!r}: {exc}"
            ) from exc
        return ProcessTransferSource(process, self._resource_name, kind)

    def open_destination(self, path: str, kind: TransferPayloadKind) -> ProcessTransferDestination:
        process = self._start("stage-file" if kind == "file" else "stage-tree")
        destination = ProcessTransferDestination(process, self._resource_name, self._invoke_control)
        try:
            destination.write(_request_bytes({"path": path}))
            destination.start()
        except BaseException as error:
            error.add_note(
                f"resource {self._resource_name!r}: staging startup for destination {path!r} failed"
            )
            for cleanup in (destination.cancel, destination.abort):
                try:
                    cleanup()
                except Exception as cleanup_error:  # noqa: BLE001 - preserve startup failure
                    error.add_note(
                        f"destination startup cleanup also failed: {format_error(cleanup_error)}"
                    )
            raise
        return destination
