from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Sequence
from dataclasses import asdict
from functools import wraps
from pathlib import Path
from typing import Annotated, Literal, ParamSpec, TypeVar

import typer
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

from ridge.application import RidgeService
from ridge.errors import RidgeError, format_error
from ridge.model import CopyResult as DomainCopyResult
from ridge.model import (
    FileStat,
    Job,
    JobLog,
    JobScope,
    ListEntry,
    ObjectEntry,
    ObjectStat,
    Operation,
    PropertyScalar,
    ResourceInspection,
)

_INLINE_CONTENT_BYTES = 64 * 1024
_EXEC_STREAM_BYTES = 32 * 1024
_DEFAULT_LIST_LIMIT = 100
_MAX_LIST_LIMIT = 200

_P = ParamSpec("_P")
_R = TypeVar("_R")


class _WireModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class ResourcePropertyResult(_WireModel):
    value: PropertyScalar
    source: Literal["configured", "detected"]


class ResourceResult(_WireModel):
    name: str
    provider: str
    addressing: Literal["filesystem", "object"] | None
    supports_copy: bool
    supported_operations: list[str]
    allowed_operations: list[str]
    background_operations: list[str]
    properties: dict[str, ResourcePropertyResult]


class ResourceSummary(_WireModel):
    name: str
    provider: str
    addressing: Literal["filesystem", "object"] | None
    supports_copy: bool
    supported_operations: list[str]
    allowed_operations: list[str]
    background_operations: list[str]


class ResourcesResult(_WireModel):
    resources: list[ResourceSummary]


class FileEntryResult(_WireModel):
    path: str
    kind: Literal["file", "directory", "symlink", "other"]
    size: int


class ObjectEntryResult(_WireModel):
    key: str
    size: int
    etag: str
    modified_at: str | None


class DataListResult(_WireModel):
    addressing: Literal["filesystem", "object"]
    entries: list[FileEntryResult | ObjectEntryResult]
    next_cursor: str | None


class ContentResult(_WireModel):
    kind: Literal["text", "binary", "too_large"]
    size: int
    content: str | None
    guidance: str | None


class WriteResult(_WireModel):
    bytes_written: int


class FileStatResult(_WireModel):
    path: str
    kind: Literal["file", "directory", "symlink", "other"]
    size: int
    modified_ns: int


class ObjectStatResult(_WireModel):
    key: str
    size: int
    etag: str
    modified_at: str | None


class DataStatResult(_WireModel):
    addressing: Literal["filesystem", "object"]
    metadata: FileStatResult | ObjectStatResult


class CopyResult(_WireModel):
    bytes_copied: int
    entries_copied: int
    duration_seconds: float


class ExecStreamResult(_WireModel):
    kind: Literal["text", "binary"]
    size: int
    content: str | None
    truncated: bool


class ExecuteResult(_WireModel):
    argv: list[str]
    exit_code: int
    stdout: ExecStreamResult
    stderr: ExecStreamResult
    duration_seconds: float


class JobScopeResult(_WireModel):
    resource: str
    operation: str


class LockResult(_WireModel):
    id: str
    kind: Literal["session", "operation"]
    status: Literal["open", "closing", "active", "uncertain", "released"]
    scopes: list[JobScopeResult]
    claims: dict[str, Literal["shared", "exclusive"]]
    expires_at: float | None = None
    lease_seconds: float | None = None
    session_id: str | None = None
    job_id: str | None = None
    reason: str | None = None


class LockAcquisitionResult(LockResult):
    token: str


class LocksResult(_WireModel):
    entries: list[LockResult]
    next_cursor: str | None


class JobSummaryResult(_WireModel):
    id: str
    kind: str
    status: str
    scopes: list[JobScopeResult]
    submitted_at: str
    started_at: str | None
    finished_at: str | None


class JobResult(JobSummaryResult):
    error: str | None
    result: dict[str, object] | None
    cancellation_requested: bool


class JobsResult(_WireModel):
    jobs: list[JobSummaryResult]
    next_cursor: str | None


class JobLogResult(_WireModel):
    job_id: str
    stream: Literal["stdout", "stderr"]
    offset: int
    next_offset: int
    complete: bool
    kind: Literal["text", "binary"]
    content: str


class ExecuteOperationResult(_WireModel):
    mode: Literal["completed", "submitted"]
    result: ExecuteResult | None
    job: JobResult | None


class WriteOperationResult(_WireModel):
    mode: Literal["completed", "submitted"]
    result: WriteResult | None
    job: JobResult | None


class CopyOperationResult(_WireModel):
    mode: Literal["completed", "submitted"]
    result: CopyResult | None
    job: JobResult | None


def _tool_errors(function: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return function(*args, **kwargs)
        except ToolError:
            raise
        except (RidgeError, OSError, ValueError) as exc:
            raise ToolError(format_error(exc)) from exc

    return wrapped


def _read_result(content: bytes) -> ContentResult:
    if len(content) > _INLINE_CONTENT_BYTES:
        return _too_large_result(len(content))
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return ContentResult(
            kind="binary",
            size=len(content),
            content=None,
            guidance="Use copy to move binary content without adding it to model context.",
        )
    return ContentResult(kind="text", size=len(content), content=text, guidance=None)


def _too_large_result(size: int) -> ContentResult:
    return ContentResult(
        kind="too_large",
        size=size,
        content=None,
        guidance="Use copy to move this content without adding it to model context.",
    )


def _exec_stream(content: bytes) -> ExecStreamResult:
    truncated = len(content) > _EXEC_STREAM_BYTES
    prefix = content[:_EXEC_STREAM_BYTES]
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return ExecStreamResult(
            kind="binary",
            size=len(content),
            content=None,
            truncated=truncated,
        )
    return ExecStreamResult(
        kind="text",
        size=len(content),
        content=prefix.decode("utf-8", errors="ignore"),
        truncated=truncated,
    )


def _decode_content(content: str, encoding: Literal["utf-8", "base64"]) -> bytes:
    if encoding == "utf-8":
        return content.encode("utf-8")
    try:
        return base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolError("content is not valid base64") from exc


def _resource_result(inspection: ResourceInspection) -> ResourceResult:
    return ResourceResult(
        name=inspection.name,
        provider=inspection.provider,
        addressing=inspection.addressing,
        supports_copy=inspection.supports_copy,
        supported_operations=[operation.value for operation in inspection.supported_operations],
        allowed_operations=[operation.value for operation in inspection.allowed_operations],
        background_operations=[operation.value for operation in inspection.background_operations],
        properties={
            name: ResourcePropertyResult(value=property_.value, source=property_.source)
            for name, property_ in inspection.properties.items()
        },
    )


def _resource_summary(inspection: ResourceInspection) -> ResourceSummary:
    return ResourceSummary(
        name=inspection.name,
        provider=inspection.provider,
        addressing=inspection.addressing,
        supports_copy=inspection.supports_copy,
        supported_operations=[operation.value for operation in inspection.supported_operations],
        allowed_operations=[operation.value for operation in inspection.allowed_operations],
        background_operations=[operation.value for operation in inspection.background_operations],
    )


def _job_result(job: Job) -> JobResult:
    return JobResult(
        id=job.id,
        kind=job.kind.value,
        status=job.status.value,
        scopes=[
            JobScopeResult(resource=scope.resource, operation=scope.operation.value)
            for scope in job.scopes
        ],
        submitted_at=job.submitted_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error=job.error,
        result=job.result,
        cancellation_requested=job.cancellation_requested,
    )


def _job_log_result(log: JobLog) -> JobLogResult:
    try:
        content = log.content.decode("utf-8")
        kind: Literal["text", "binary"] = "text"
    except UnicodeDecodeError:
        content = base64.b64encode(log.content).decode("ascii")
        kind = "binary"
    return JobLogResult(
        job_id=log.job_id,
        stream=log.stream,
        offset=log.offset,
        next_offset=log.next_offset,
        complete=log.complete,
        kind=kind,
        content=content,
    )


def _file_entry_result(entry: ListEntry) -> FileEntryResult:
    return FileEntryResult(path=entry.path, kind=entry.kind, size=entry.size)


def _file_stat_result(stat: FileStat) -> FileStatResult:
    return FileStatResult(
        path=stat.path,
        kind=stat.kind,
        size=stat.size,
        modified_ns=stat.modified_ns,
    )


def _object_entry_result(entry: ObjectEntry) -> ObjectEntryResult:
    return ObjectEntryResult(
        key=entry.key,
        size=entry.size,
        etag=entry.etag,
        modified_at=entry.modified_at,
    )


def _object_stat_result(stat: ObjectStat) -> ObjectStatResult:
    return ObjectStatResult(
        key=stat.key,
        size=stat.size,
        etag=stat.etag,
        modified_at=stat.modified_at,
    )


def _copy_result(result: DomainCopyResult) -> CopyResult:
    return CopyResult(
        bytes_copied=result.bytes_copied,
        entries_copied=result.entries_copied,
        duration_seconds=result.duration_seconds,
    )


_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=True,
)
_EXECUTE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)


def create_server(service: RidgeService) -> MCPServer[None]:
    server = MCPServer(
        "ridge",
        version="0.1.0",
        instructions=(
            "Use list_resources to discover capabilities; call inspect_resource only when detailed "
            "properties are needed. Paths and object keys are relative to each configured resource. "
            "Use copy for large or binary content. Pass commands as an argv array, never as a shell "
            "command string. Set background=true when duration is uncertain, incremental logs or "
            "cancellation matter, or a synchronous tool timeout is likely. Do not retry a submission "
            "without an idempotency_key. Ordinary operations acquire resource claims automatically. "
            "Use acquire_locks to reserve resources across calls, pass lock_token on each operation, "
            "and renew before lease expiry. Inspect outstanding claims after conflicts; uncertain "
            "work may still be running. Release sessions when finished."
        ),
    )

    @server.tool(annotations=_READ_ONLY, structured_output=True)
    @_tool_errors
    def list_resources() -> ResourcesResult:
        """List concise resource summaries and operations; inspect only when properties are needed."""
        return ResourcesResult(
            resources=[_resource_summary(item) for item in service.list_resources()]
        )

    @server.tool(annotations=_READ_ONLY, structured_output=True)
    @_tool_errors
    def inspect_resource(resource: str) -> ResourceResult:
        """Inspect one configured resource and its capabilities."""
        return _resource_result(service.inspect_resource(resource))

    @server.tool(annotations=_EXECUTE, structured_output=True)
    @_tool_errors
    def execute(
        resource: str,
        argv: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        background: bool = False,
        idempotency_key: str | None = None,
        lock_token: str | None = None,
    ) -> ExecuteOperationResult:
        """Execute argv on a compute resource with bounded model-facing output."""
        if background:
            return ExecuteOperationResult(
                mode="submitted",
                result=None,
                job=_job_result(
                    service.with_lock(lock_token).submit_execution(
                        resource,
                        argv,
                        cwd=cwd,
                        env=env,
                        timeout_seconds=timeout_seconds,
                        idempotency_key=idempotency_key,
                    )
                ),
            )
        if idempotency_key is not None:
            raise ToolError("idempotency_key requires background=true")
        result = service.with_lock(lock_token).execute(
            resource,
            argv,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )
        return ExecuteOperationResult(
            mode="completed",
            result=ExecuteResult(
                argv=list(result.argv),
                exit_code=result.exit_code,
                stdout=_exec_stream(result.stdout),
                stderr=_exec_stream(result.stderr),
                duration_seconds=result.duration_seconds,
            ),
            job=None,
        )

    @server.tool(annotations=_READ_ONLY, structured_output=True)
    @_tool_errors
    def list_data(
        resource: str,
        path: str | None = None,
        cursor: str | None = None,
        limit: int = _DEFAULT_LIST_LIMIT,
        lock_token: str | None = None,
    ) -> DataListResult:
        """List directory children or exact object-prefix matches; inspect addressing first."""
        if not 1 <= limit <= _MAX_LIST_LIMIT:
            raise ToolError(f"limit must be between 1 and {_MAX_LIST_LIMIT}")
        page = service.with_lock(lock_token).list_data(resource, path, cursor=cursor, limit=limit)
        return DataListResult(
            addressing=page.addressing,
            entries=[
                _file_entry_result(entry)
                if isinstance(entry, ListEntry)
                else _object_entry_result(entry)
                for entry in page.entries
            ],
            next_cursor=page.next_cursor,
        )

    @server.tool(annotations=_READ_ONLY, structured_output=True)
    @_tool_errors
    def read_data(resource: str, path: str, lock_token: str | None = None) -> ContentResult:
        """Stat then read a small UTF-8 file/object; requires data.stat and data.read."""
        stat = service.with_lock(lock_token).stat_data(resource, path)
        if stat.size > _INLINE_CONTENT_BYTES:
            return _too_large_result(stat.size)
        return _read_result(
            service.with_lock(lock_token).read_data(resource, path, max_bytes=_INLINE_CONTENT_BYTES)
        )

    @server.tool(annotations=_WRITE, structured_output=True)
    @_tool_errors
    def write_data(
        resource: str,
        path: str,
        content: str,
        encoding: Literal["utf-8", "base64"] = "utf-8",
        background: bool = False,
        idempotency_key: str | None = None,
        lock_token: str | None = None,
    ) -> WriteOperationResult:
        """Create or replace a file/object from buffered UTF-8 text or base64 bytes."""
        decoded = _decode_content(content, encoding)
        if background:
            return WriteOperationResult(
                mode="submitted",
                result=None,
                job=_job_result(
                    service.with_lock(lock_token).submit_write(
                        resource, path, decoded, idempotency_key=idempotency_key
                    )
                ),
            )
        if idempotency_key is not None:
            raise ToolError("idempotency_key requires background=true")
        service.with_lock(lock_token).write_data(resource, path, decoded)
        return WriteOperationResult(
            mode="completed",
            result=WriteResult(bytes_written=len(decoded)),
            job=None,
        )

    @server.tool(annotations=_READ_ONLY, structured_output=True)
    @_tool_errors
    def stat_data(resource: str, path: str, lock_token: str | None = None) -> DataStatResult:
        """Inspect a resource-relative filesystem path or exact object key."""
        stat = service.with_lock(lock_token).stat_data(resource, path)
        return DataStatResult(
            addressing="filesystem" if isinstance(stat, FileStat) else "object",
            metadata=_file_stat_result(stat)
            if isinstance(stat, FileStat)
            else _object_stat_result(stat),
        )

    @server.tool(annotations=_WRITE, structured_output=True)
    @_tool_errors
    def copy(
        source: str,
        destination: str,
        background: bool = False,
        idempotency_key: str | None = None,
        lock_token: str | None = None,
    ) -> CopyOperationResult:
        """Copy a file or directory tree between exact RESOURCE:PATH locations."""
        if background:
            return CopyOperationResult(
                mode="submitted",
                result=None,
                job=_job_result(
                    service.with_lock(lock_token).submit_copy(
                        source, destination, idempotency_key=idempotency_key
                    )
                ),
            )
        if idempotency_key is not None:
            raise ToolError("idempotency_key requires background=true")
        return CopyOperationResult(
            mode="completed",
            result=_copy_result(service.with_lock(lock_token).copy(source, destination)),
            job=None,
        )

    @server.tool(annotations=_READ_ONLY, structured_output=True)
    @_tool_errors
    def list_jobs(limit: int = 50, cursor: str | None = None) -> JobsResult:
        """Page authorized job summaries, newest first; limit 1–200. Inspect for results/errors."""
        page = service.list_jobs(limit=limit, cursor=cursor)
        return JobsResult(
            jobs=[JobSummaryResult.model_validate(asdict(job)) for job in page.jobs],
            next_cursor=page.next_cursor,
        )

    @server.tool(annotations=_READ_ONLY, structured_output=True)
    @_tool_errors
    def inspect_job(job_id: str) -> JobResult:
        """Inspect status and result metadata for one durable job."""
        return _job_result(service.inspect_job(job_id))

    @server.tool(annotations=_READ_ONLY, structured_output=True)
    @_tool_errors
    def read_job_logs(
        job_id: str,
        stream: Literal["stdout", "stderr"] = "stdout",
        offset: int = 0,
        limit: int = 64 * 1024,
    ) -> JobLogResult:
        """Read a bounded byte range from one job log; binary content is base64 encoded."""
        return _job_log_result(service.read_job_log(job_id, stream, offset=offset, limit=limit))

    @server.tool(annotations=_WRITE, structured_output=True)
    @_tool_errors
    def cancel_job(job_id: str) -> JobResult:
        """Request cancellation of a running job and return its observed state."""
        return _job_result(service.cancel_job(job_id))

    @server.tool(annotations=_EXECUTE, structured_output=True)
    @_tool_errors
    def acquire_locks(
        scopes: list[JobScopeResult], lease_seconds: float = 300, wait_seconds: float = 0
    ) -> LockAcquisitionResult:
        """Reserve all declared resource/operation pairs; return a secret session token."""
        return LockAcquisitionResult.model_validate(
            service.acquire_locks(
                [JobScope(s.resource, Operation(s.operation)) for s in scopes],
                lease_seconds=lease_seconds,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(annotations=_EXECUTE, structured_output=True)
    @_tool_errors
    def renew_locks(token: str) -> LockResult:
        """Renew an open idle-session lease; expired tokens cannot be revived."""
        return LockResult.model_validate(service.renew_locks(token))

    @server.tool(annotations=_WRITE, structured_output=True)
    @_tool_errors
    def release_locks(token: str) -> LockResult:
        """Close session admission; retain reservations while operations remain active."""
        return LockResult.model_validate(service.release_locks(token))

    @server.tool(annotations=_READ_ONLY, structured_output=True)
    @_tool_errors
    def inspect_lock(identity: str) -> LockResult:
        """Inspect a session or operation without exposing its ownership token."""
        return LockResult.model_validate(service.inspect_lock(identity))

    @server.tool(annotations=_READ_ONLY, structured_output=True)
    @_tool_errors
    def list_locks(cursor: str | None = None, limit: int = 100) -> LocksResult:
        """List a bounded page of authorized outstanding sessions and operations."""
        return LocksResult.model_validate(service.list_locks(cursor=cursor, limit=limit))

    @server.tool(annotations=_WRITE, structured_output=True)
    @_tool_errors
    def force_release_lock(identity: str, reason: str) -> LockResult:
        """Release uncertain operation claims after external inspection; does not cancel work."""
        return LockResult.model_validate(service.force_release_lock(identity, reason=reason))

    _registered_tools = (
        acquire_locks,
        renew_locks,
        release_locks,
        inspect_lock,
        list_locks,
        force_release_lock,
        list_resources,
        inspect_resource,
        execute,
        list_data,
        read_data,
        write_data,
        stat_data,
        copy,
        list_jobs,
        inspect_job,
        read_job_logs,
        cancel_job,
    )
    del _registered_tools
    return server


app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    pretty_exceptions_enable=False,
    help="Serve configured Ridge resources over MCP stdio.",
)


@app.callback()
def serve(
    config: Annotated[
        Path,
        typer.Option("--config", envvar="RIDGE_CONFIG", help="Resource configuration."),
    ] = Path("ridge.yaml"),
) -> None:
    """Run the Ridge MCP server over stdio."""
    create_server(RidgeService.from_config(config)).run()


def main(argv: Sequence[str] | None = None) -> None:
    app(args=list(argv) if argv is not None else None, prog_name="ridge-mcp")


if __name__ == "__main__":
    main()
