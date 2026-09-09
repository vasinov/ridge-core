from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from functools import wraps
from pathlib import Path
from typing import Annotated, ParamSpec, TypeVar, cast

import typer

from ridge.application import RidgeService
from ridge.config import load_configuration
from ridge.errors import ExecutionTimeoutError, RidgeError, format_error
from ridge.model import JobScope, Operation

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Execute and access data on named resources.",
)
jobs_app = typer.Typer(no_args_is_help=True, help="Observe and cancel durable background jobs.")
app.add_typer(jobs_app, name="jobs")
locks_app = typer.Typer(no_args_is_help=True, help="Coordinate resource access across callers.")
app.add_typer(locks_app, name="locks")
config_app = typer.Typer(
    no_args_is_help=True, help="Check the resource inventory without running it."
)
app.add_typer(config_app, name="config")

_P = ParamSpec("_P")
_R = TypeVar("_R")


@dataclass(frozen=True, slots=True)
class _AppState:
    config: Path
    lock_token: str | None = None


def _handle_errors(function: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return function(*args, **kwargs)
        except ExecutionTimeoutError as exc:
            typer.echo(f"ridge: {format_error(exc)}", err=True)
            raise typer.Exit(code=124) from exc
        except (RidgeError, OSError, ValueError) as exc:
            typer.echo(f"ridge: {format_error(exc)}", err=True)
            raise typer.Exit(code=2) from exc
        except KeyboardInterrupt as exc:
            if getattr(exc, "__notes__", ()):
                typer.echo(f"ridge: interrupted\n{format_error(exc)}", err=True)
            raise

    return wrapped


@app.callback()
def configure(
    ctx: typer.Context,
    config: Annotated[
        Path,
        typer.Option(
            "--config",
            envvar="RIDGE_CONFIG",
            help="Resource configuration.",
        ),
    ] = Path("ridge.yaml"),
    lock_token: Annotated[
        str | None,
        typer.Option(
            "--lock-token",
            envvar="RIDGE_LOCK_TOKEN",
            help="Explicit session token for resource operations.",
        ),
    ] = None,
) -> None:
    """Configure the Ridge command invocation."""
    ctx.obj = _AppState(config=config, lock_token=lock_token)


def _service(ctx: typer.Context) -> RidgeService:
    state = cast(_AppState, ctx.obj)
    return RidgeService.from_config(state.config).with_lock(state.lock_token)


@config_app.command("validate")
def config_validate(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit one JSON result on stdout, including validation errors."),
    ] = False,
) -> None:
    """Validate with the runtime loader; no connectivity probes or Ridge state creation.

    Exit 0 for valid configuration or 2 for the first error. Provider constructors
    are trusted code. Diagnostics may contain sensitive paths or input values.
    """
    state = cast(_AppState, ctx.obj)
    try:
        loaded = load_configuration(state.config)
    except (RidgeError, OSError, ValueError) as exc:
        message = format_error(exc)
        if json_output:
            typer.echo(json.dumps({"valid": False, "error": message}))
        else:
            typer.echo(f"ridge: {message}", err=True)
        raise typer.Exit(code=2) from exc

    # Do not use service construction or inspect_properties: neither is needed
    # to summarize the loaded policy, and they may initialize state or probe targets.
    resources = [
        {
            "name": name,
            "provider": loaded.registry.get(name).provider_name,
            "lock_key": loaded.lock_keys[name],
            "allowed_operations": [
                operation.value
                for operation in loaded.registry.get(name).capabilities.operations
                if loaded.authorization.allows(name, operation)
            ],
            "delegable_operations": [
                operation.value
                for operation in loaded.registry.get(name).capabilities.operations
                if loaded.authorization.allows(name, operation)
                and loaded.delegation.allows(name, operation)
            ],
        }
        for name in loaded.registry.names()
    ]
    permission_mode = "unrestricted" if loaded.authorization.unrestricted_mode else "exact"
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "valid": True,
                    "config": str(loaded.path),
                    "state_directory": str(loaded.state_directory),
                    "permission_mode": permission_mode,
                    "resources": resources,
                }
            )
        )
    else:
        typer.echo(f"Valid configuration: {loaded.path}")
        typer.echo(f"State directory: {loaded.state_directory} (not initialized)")
        typer.echo(f"Permissions: {permission_mode}")
        for resource in resources:
            typer.echo(json.dumps(resource))


@locks_app.command("acquire")
@_handle_errors
def locks_acquire(
    ctx: typer.Context,
    scopes: Annotated[list[str], typer.Argument(help="RESOURCE:OPERATION pairs.")],
    lease_seconds: float = 300,
    wait_seconds: float = 0,
) -> None:
    declared: list[JobScope] = []
    for value in scopes:
        resource, separator, operation = value.partition(":")
        if not separator:
            raise ValueError("scope must be RESOURCE:OPERATION")
        declared.append(JobScope(resource, Operation(operation)))
    typer.echo(
        json.dumps(
            _service(ctx).acquire_locks(
                declared, lease_seconds=lease_seconds, wait_seconds=wait_seconds
            )
        )
    )


def _token(ctx: typer.Context) -> str:
    token = cast(_AppState, ctx.obj).lock_token
    if token is None:
        raise ValueError("provide --lock-token or RIDGE_LOCK_TOKEN")
    return token


@locks_app.command("renew")
@_handle_errors
def locks_renew(ctx: typer.Context) -> None:
    typer.echo(json.dumps(_service(ctx).renew_locks(_token(ctx))))


@locks_app.command("release")
@_handle_errors
def locks_release(ctx: typer.Context) -> None:
    typer.echo(json.dumps(_service(ctx).release_locks(_token(ctx))))


@locks_app.command("inspect")
@_handle_errors
def locks_inspect(ctx: typer.Context, identity: str) -> None:
    typer.echo(json.dumps(_service(ctx).inspect_lock(identity)))


@locks_app.command("list")
@_handle_errors
def locks_list(ctx: typer.Context, cursor: str | None = None, limit: int = 100) -> None:
    typer.echo(json.dumps(_service(ctx).list_locks(cursor=cursor, limit=limit)))


@locks_app.command("force-release")
@_handle_errors
def locks_force_release(
    ctx: typer.Context,
    identity: str,
    reason: Annotated[
        str,
        typer.Option(help="Why it is acceptable to release uncertain work; does not cancel it."),
    ],
) -> None:
    typer.echo(json.dumps(_service(ctx).force_release_lock(identity, reason=reason)))


def _write_content(text: str | None, source_path: Path | None) -> bytes:
    if text is not None and source_path is not None:
        raise RidgeError("--text and --from are mutually exclusive")
    if text is not None:
        return text.encode()
    if source_path is not None:
        return source_path.read_bytes()
    return typer.get_binary_stream("stdin").read()


@app.command("resources")
@_handle_errors
def resources_command(ctx: typer.Context) -> None:
    """List configured resources and their supported and allowed operations."""
    typer.echo("NAME\tPROVIDER\tADDRESSING\tCOPY\tSUPPORTED\tALLOWED\tBACKGROUND")
    for inspection in _service(ctx).list_resources():
        supported = ",".join(operation.value for operation in inspection.supported_operations)
        allowed = ",".join(operation.value for operation in inspection.allowed_operations)
        background = ",".join(operation.value for operation in inspection.background_operations)
        typer.echo(
            f"{inspection.name}\t{inspection.provider}\t{inspection.addressing or '-'}\t"
            f"{inspection.supports_copy}\t{supported}\t{allowed}\t{background}"
        )


@app.command("inspect")
@_handle_errors
def inspect_command(ctx: typer.Context, resource: str) -> None:
    """Inspect one resource's operations and properties."""
    inspection = _service(ctx).inspect_resource(resource)
    payload = asdict(inspection)
    payload["supported_operations"] = [
        operation.value for operation in inspection.supported_operations
    ]
    payload["allowed_operations"] = [operation.value for operation in inspection.allowed_operations]
    payload["background_operations"] = [
        operation.value for operation in inspection.background_operations
    ]
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command(
    "exec",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
@_handle_errors
def exec_command(
    ctx: typer.Context,
    resource: str,
    cwd: Annotated[str | None, typer.Option(help="Resource-relative working directory.")] = None,
    timeout: Annotated[
        float | None, typer.Option(min=0, help="Execution timeout in seconds.")
    ] = None,
    background: Annotated[bool, typer.Option(help="Submit a durable job.")] = False,
    idempotency_key: Annotated[
        str | None, typer.Option(help="Deduplicate a retried background submission.")
    ] = None,
) -> None:
    """Execute ARGV on a compute resource; put ARGV after '--'."""
    command = list(ctx.args)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise RidgeError("exec requires a command after '--'")
    service = _service(ctx)
    if background:
        job = service.submit_execution(
            resource,
            command,
            cwd=cwd,
            timeout_seconds=timeout,
            idempotency_key=idempotency_key,
        )
        typer.echo(f"submitted {job.id}")
        return
    if idempotency_key is not None:
        raise RidgeError("--idempotency-key requires --background")
    result = service.execute(resource, command, cwd=cwd, timeout_seconds=timeout)
    typer.get_binary_stream("stdout").write(result.stdout)
    typer.get_binary_stream("stderr").write(result.stderr)
    if result.exit_code:
        raise typer.Exit(code=result.exit_code)


@app.command("list")
@_handle_errors
def list_command(
    ctx: typer.Context,
    resource: str,
    path: Annotated[
        str | None, typer.Argument(help="Relative directory or exact key prefix.")
    ] = None,
    cursor: Annotated[str | None, typer.Option(help="Opaque continuation token.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000, help="Maximum entries to return.")] = 100,
) -> None:
    """List one page of directory children or object-prefix matches."""
    page = _service(ctx).list_data(resource, path, cursor=cursor, limit=limit)
    typer.echo(json.dumps(asdict(page), sort_keys=True))


@app.command("read")
@_handle_errors
def read_command(
    ctx: typer.Context,
    resource: str,
    path: str,
    max_bytes: Annotated[int | None, typer.Option(help="Reject a larger result.")] = None,
) -> None:
    """Read a file or object's bytes to stdout (buffered)."""
    content = _service(ctx).read_data(resource, path, max_bytes=max_bytes)
    typer.get_binary_stream("stdout").write(content)


@app.command("write")
@_handle_errors
def write_command(
    ctx: typer.Context,
    resource: str,
    path: str,
    text: Annotated[str | None, typer.Option(help="UTF-8 text to write.")] = None,
    source_path: Annotated[
        Path | None,
        typer.Option("--from", exists=True, dir_okay=False, readable=True, help="File to read."),
    ] = None,
    background: Annotated[bool, typer.Option(help="Submit a durable job.")] = False,
    idempotency_key: Annotated[
        str | None, typer.Option(help="Deduplicate a retried background submission.")
    ] = None,
) -> None:
    """Write bytes from stdin, --text, or --from."""
    content = _write_content(text, source_path)
    service = _service(ctx)
    if background:
        job = service.submit_write(resource, path, content, idempotency_key=idempotency_key)
        typer.echo(f"submitted {job.id}")
        return
    if idempotency_key is not None:
        raise RidgeError("--idempotency-key requires --background")
    service.write_data(resource, path, content)


@app.command("delete")
@_handle_errors
def delete_command(
    ctx: typer.Context,
    resource: str,
    path: str,
    recursive: Annotated[
        bool, typer.Option(help="Delete a nonempty directory tree; no rollback.")
    ] = False,
    background: Annotated[bool, typer.Option(help="Submit a durable job.")] = False,
    idempotency_key: Annotated[
        str | None, typer.Option(help="Deduplicate a retried background submission.")
    ] = None,
) -> None:
    """Delete an exact path/key, never the filesystem resource root. Missing targets succeed."""
    service = _service(ctx)
    if background:
        job = service.submit_delete(
            resource, path, recursive=recursive, idempotency_key=idempotency_key
        )
        typer.echo(f"submitted {job.id}")
        return
    if idempotency_key is not None:
        raise RidgeError("--idempotency-key requires --background")
    typer.echo(json.dumps(asdict(service.delete_data(resource, path, recursive=recursive))))


@app.command("stat")
@_handle_errors
def stat_command(ctx: typer.Context, resource: str, path: str) -> None:
    """Inspect a filesystem path or exact object key."""
    typer.echo(json.dumps(asdict(_service(ctx).stat_data(resource, path))))


@app.command("copy")
@_handle_errors
def copy_command(
    ctx: typer.Context,
    source: str,
    destination: str,
    background: Annotated[bool, typer.Option(help="Submit a durable job.")] = False,
    idempotency_key: Annotated[
        str | None, typer.Option(help="Deduplicate a retried background submission.")
    ] = None,
) -> None:
    """Copy a file or directory tree between exact resource locations."""
    service = _service(ctx)
    if background:
        job = service.submit_copy(source, destination, idempotency_key=idempotency_key)
        typer.echo(f"submitted {job.id}")
        return
    if idempotency_key is not None:
        raise RidgeError("--idempotency-key requires --background")
    result = service.copy(source, destination)
    entry_label = "entry" if result.entries_copied == 1 else "entries"
    typer.echo(f"copied {result.bytes_copied} bytes in {result.entries_copied} {entry_label}")


@jobs_app.command("list")
@_handle_errors
def jobs_list_command(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option(help="Page size, 1–200.")] = 50,
    cursor: Annotated[str | None, typer.Option(help="Opaque continuation token.")] = None,
) -> None:
    """Return a JSON page of authorized job summaries, newest first."""
    typer.echo(json.dumps(asdict(_service(ctx).list_jobs(limit=limit, cursor=cursor))))


@jobs_app.command("inspect")
@_handle_errors
def jobs_inspect_command(ctx: typer.Context, job_id: str) -> None:
    """Inspect one durable job."""
    typer.echo(json.dumps(asdict(_service(ctx).inspect_job(job_id)), indent=2, sort_keys=True))


@jobs_app.command("logs")
@_handle_errors
def jobs_logs_command(
    ctx: typer.Context,
    job_id: str,
    stream: Annotated[str, typer.Option(help="stdout or stderr.")] = "stdout",
    offset: Annotated[int, typer.Option(min=0, help="Byte offset.")] = 0,
    limit: Annotated[
        int, typer.Option(min=1, max=1024 * 1024, help="Maximum bytes to return.")
    ] = 64 * 1024,
) -> None:
    """Read a bounded byte range from a job log."""
    log = _service(ctx).read_job_log(job_id, stream, offset=offset, limit=limit)
    typer.get_binary_stream("stdout").write(log.content)


@jobs_app.command("cancel")
@_handle_errors
def jobs_cancel_command(ctx: typer.Context, job_id: str) -> None:
    """Request cancellation and report the resulting observed state."""
    job = _service(ctx).cancel_job(job_id)
    typer.echo(f"{job.id}\t{job.status.value}")


def main(argv: Sequence[str] | None = None) -> None:
    app(args=list(argv) if argv is not None else None, prog_name="ridge")
