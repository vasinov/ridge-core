from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from copy import copy as shallow_copy
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, Literal, Never, cast

from ridge.authorization import AuthorizationPolicy, AuthorizationRequest, Authorizer
from ridge.config import load_configuration
from ridge.coordination import Coordination
from ridge.errors import AuthorizationDeniedError, JobsUnavailableError, UnsupportedOperationError
from ridge.jobs import JobManager
from ridge.model import (
    CopyRequest,
    CopyResult,
    DataPage,
    ExecResult,
    FileStat,
    Job,
    JobKind,
    JobLog,
    JobScope,
    ObjectStat,
    Operation,
    ResourceInspection,
    ResourceLocation,
)
from ridge.registry import ResourceRegistry
from ridge.resource import (
    ComputeCapability,
    Resource,
    ResourceCapabilities,
    StreamingComputeCapability,
)
from ridge.transfer import copy


class RidgeService:
    """Frontend-neutral Ridge workflows over a resource registry."""

    def __init__(
        self,
        registry: ResourceRegistry,
        authorization: Authorizer | None = None,
        jobs: JobManager | None = None,
    ) -> None:
        self._registry = registry
        self._authorization = authorization or AuthorizationPolicy.unrestricted()
        self._jobs = jobs
        self._lock_token: str | None = None

    @classmethod
    def from_config(cls, config_path: str | Path) -> RidgeService:
        loaded = load_configuration(config_path)
        if loaded.path is None or loaded.fingerprint is None or loaded.state_directory is None:
            return cls(loaded.registry, loaded.authorization)
        return cls(
            loaded.registry,
            loaded.authorization,
            JobManager(
                loaded.state_directory, loaded.path, loaded.fingerprint, lock_keys=loaded.lock_keys
            ),
        )

    def with_lock(self, token: str | None) -> RidgeService:
        """Return a request-scoped service using an explicit coordination session."""
        service = shallow_copy(self)
        service._lock_token = token
        return service

    def _coordination(self) -> Coordination:
        return self._job_manager().coordination

    def _local_scopes(self, scopes: Sequence[JobScope]) -> bool:
        return all(self._registry.get(s.resource).provider_name == "local" for s in scopes)

    def _authorize_scopes(self, scopes: Sequence[JobScope]) -> None:
        if not 1 <= len(scopes) <= 100:
            raise ValueError("declare between 1 and 100 resource/operation pairs")
        for scope in scopes:
            target = self._registry.get(scope.resource)
            if scope.operation not in target.capabilities.operations:
                self._unsupported(target, scope.operation)
            self._authorize(scope.resource, scope.operation, {"coordination": True})

    def acquire_locks(
        self, scopes: Sequence[JobScope], *, lease_seconds: float = 300, wait_seconds: float = 0
    ) -> dict[str, object]:
        self._authorize_scopes(scopes)
        self._reconcile_jobs()
        return self._coordination().acquire(
            scopes, lease_seconds=lease_seconds, wait_seconds=wait_seconds
        )

    def renew_locks(self, token: str) -> dict[str, object]:
        self._authorize_scopes(self._coordination().token_scopes(token))
        return self._coordination().session_action(token)

    def release_locks(self, token: str) -> dict[str, object]:
        return self._coordination().session_action(token, release=True)

    @staticmethod
    def _lock_scopes(value: Mapping[str, object]) -> tuple[JobScope, ...]:
        return tuple(
            JobScope(s["resource"], Operation(s["operation"]))
            for s in cast(list[dict[str, str]], value["scopes"])
        )

    def inspect_lock(self, identity: str) -> dict[str, object]:
        self._reconcile_jobs()
        value = self._coordination().inspect(identity)
        self._authorize_scopes(self._lock_scopes(value))
        return value

    def force_release_lock(self, identity: str, *, reason: str) -> dict[str, object]:
        self.inspect_lock(identity)
        self._coordination().force_release(identity, reason)
        return self.inspect_lock(identity)

    def list_locks(self, *, cursor: str | None = None, limit: int = 100) -> dict[str, object]:
        if isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if cursor is not None:
            import uuid

            try:
                uuid.UUID(cursor)
            except ValueError as exc:
                raise ValueError("invalid lock cursor") from exc
        self._reconcile_jobs()
        after = cursor or ""
        entries: list[dict[str, object]] = []
        while True:
            page = self._coordination().page(after=after, limit=100)
            for value in page:
                after = str(value["id"])
                if all(
                    self._authorization.allows(s.resource, s.operation)
                    for s in self._lock_scopes(value)
                ):
                    entries.append(value)
                    if len(entries) == limit:
                        return {"entries": entries, "next_cursor": after}
            if len(page) < 100:
                return {"entries": entries, "next_cursor": None}

    def _reconcile_jobs(self) -> None:
        if self._jobs is not None:
            self._jobs.reconcile_claims()

    @contextmanager
    def _operation(self, scopes: Sequence[JobScope]) -> Generator[None]:
        from ridge._job_process import current_job

        if self._jobs is None:
            if self._lock_token is not None:
                raise JobsUnavailableError("coordination requires configured shared state")
            yield
            return
        job_id = current_job.get()
        if job_id is not None:
            self._coordination().validate_job(job_id, scopes)
            yield
            return
        self._reconcile_jobs()
        with self._coordination().operation(
            scopes, token=self._lock_token, local_only=self._local_scopes(scopes)
        ):
            yield

    def list_resources(self) -> tuple[ResourceInspection, ...]:
        return tuple(self._with_permissions(item) for item in self._registry.inspections())

    def inspect_resource(self, resource: str) -> ResourceInspection:
        return self._with_permissions(self._registry.inspect(resource))

    def execute(
        self,
        resource: str,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecResult:
        if not argv:
            raise ValueError("execute requires at least one argument")
        self._validate_timeout(timeout_seconds)
        target = self._compute(
            resource,
            Operation.COMPUTE_EXEC,
            {
                "argv": tuple(argv),
                "cwd": cwd,
                "env_names": tuple(sorted(env or {})),
                "timeout_seconds": timeout_seconds,
            },
        )
        with self._operation((JobScope(resource, Operation.COMPUTE_EXEC),)):
            return target.exec(argv, cwd=cwd, env=env, timeout_seconds=timeout_seconds)

    def submit_execution(
        self,
        resource: str,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        idempotency_key: str | None = None,
    ) -> Job:
        if not argv:
            raise ValueError("execute requires at least one argument")
        self._validate_timeout(timeout_seconds)
        if env:
            raise ValueError("background execution does not persist explicit environment values")
        self._compute(
            resource,
            Operation.COMPUTE_EXEC,
            {
                "argv": tuple(argv),
                "cwd": cwd,
                "env_names": (),
                "timeout_seconds": timeout_seconds,
                "background": True,
            },
        )
        scopes = (JobScope(resource, Operation.COMPUTE_EXEC),)
        self._reconcile_jobs()
        return self._job_manager().submit(
            JobKind.EXECUTE,
            scopes,
            {
                "resource": resource,
                "argv": list(argv),
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
            },
            idempotency_key=idempotency_key,
            lock_token=self._lock_token,
            local_only=self._local_scopes(scopes),
        )

    def _execute_to_logs(
        self,
        resource: str,
        argv: Sequence[str],
        stdout: BinaryIO,
        stderr: BinaryIO,
        *,
        cwd: str | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecResult:
        """Execute for the local job supervisor, streaming when the backend supports it."""
        if not argv:
            raise ValueError("execute requires at least one argument")
        self._validate_timeout(timeout_seconds)
        target = self._compute(
            resource,
            Operation.COMPUTE_EXEC,
            {
                "argv": tuple(argv),
                "cwd": cwd,
                "env_names": (),
                "timeout_seconds": timeout_seconds,
                "background": True,
            },
        )
        with self._operation((JobScope(resource, Operation.COMPUTE_EXEC),)):
            if isinstance(target, StreamingComputeCapability):
                return target.exec_streaming(
                    argv,
                    stdout,
                    stderr,
                    cwd=cwd,
                    timeout_seconds=timeout_seconds,
                )
            result = target.exec(argv, cwd=cwd, timeout_seconds=timeout_seconds)
            stdout.write(result.stdout)
            stderr.write(result.stderr)
            return result

    def list_data(
        self,
        resource: str,
        path: str | None = None,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> DataPage:
        """List directory children or exact prefix matches, according to addressing."""
        if isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        target = self._data(
            resource, Operation.DATA_LIST, {"path": path, "cursor": cursor, "limit": limit}
        )
        with self._operation((JobScope(resource, Operation.DATA_LIST),)):
            if target.storage is not None:
                page = target.storage.list_objects(
                    "" if path is None else path, cursor=cursor, limit=limit
                )
                return DataPage("object", page.entries, page.next_cursor)
            assert target.filesystem is not None
            directory = "." if path is None else path
            offset = 0
            if cursor is not None:
                try:
                    decoded: object = json.loads(base64.b64decode(cursor, validate=True))
                    if not isinstance(decoded, list):
                        raise TypeError
                    value = cast(list[object], decoded)
                    if (
                        len(value) != 3
                        or value[:2] != [resource, directory]
                        or type(value[2]) is not int
                        or value[2] < 0
                    ):
                        raise ValueError
                    offset = value[2]
                except (TypeError, ValueError, UnicodeError, binascii.Error) as exc:
                    raise ValueError("invalid cursor for this resource directory") from exc
            all_entries = target.filesystem.list(directory)
            entries = all_entries[offset : offset + limit]
            next_offset = offset + len(entries)
            next_cursor = (
                base64.b64encode(json.dumps([resource, directory, next_offset]).encode()).decode()
                if next_offset < len(all_entries)
                else None
            )
            return DataPage("filesystem", entries, next_cursor)

    def read_data(self, resource: str, path: str, *, max_bytes: int | None = None) -> bytes:
        target = self._data(resource, Operation.DATA_READ, {"path": path, "max_bytes": max_bytes})
        with self._operation((JobScope(resource, Operation.DATA_READ),)):
            if target.storage is not None:
                return target.storage.read_object(path, max_bytes=max_bytes)
            assert target.filesystem is not None
            return target.filesystem.read(path, max_bytes=max_bytes)

    def write_data(self, resource: str, path: str, content: bytes) -> None:
        target = self._data(resource, Operation.DATA_WRITE, {"path": path})
        with self._operation((JobScope(resource, Operation.DATA_WRITE),)):
            if target.storage is not None:
                target.storage.write_object(path, content)
            else:
                assert target.filesystem is not None
                target.filesystem.write(path, content)

    def submit_write(
        self,
        resource: str,
        path: str,
        content: bytes,
        *,
        idempotency_key: str | None = None,
    ) -> Job:
        self._data(resource, Operation.DATA_WRITE, {"path": path, "background": True})
        scopes = (JobScope(resource, Operation.DATA_WRITE),)
        self._reconcile_jobs()
        return self._job_manager().submit(
            JobKind.WRITE,
            scopes,
            {"resource": resource, "path": path},
            payload=content,
            idempotency_key=idempotency_key,
            lock_token=self._lock_token,
            local_only=self._local_scopes(scopes),
        )

    def stat_data(self, resource: str, path: str) -> FileStat | ObjectStat:
        target = self._data(resource, Operation.DATA_STAT, {"path": path})
        with self._operation((JobScope(resource, Operation.DATA_STAT),)):
            if target.storage is not None:
                return target.storage.stat_object(path)
            assert target.filesystem is not None
            return target.filesystem.stat(path)

    def copy(
        self, source: str | ResourceLocation, destination: str | ResourceLocation
    ) -> CopyResult:
        source_location = ResourceLocation.parse(source) if isinstance(source, str) else source
        destination_location = (
            ResourceLocation.parse(destination) if isinstance(destination, str) else destination
        )
        self._transfer(
            source_location.resource,
            Operation.DATA_READ,
            {"path": source_location.path, "destination": destination_location},
        )
        self._transfer(
            destination_location.resource,
            Operation.DATA_WRITE,
            {"path": destination_location.path, "source": source_location},
        )
        with self._operation(
            (
                JobScope(source_location.resource, Operation.DATA_READ),
                JobScope(destination_location.resource, Operation.DATA_WRITE),
            )
        ):
            return copy(
                self._registry,
                CopyRequest(source=source_location, destination=destination_location),
            )

    def submit_copy(
        self,
        source: str | ResourceLocation,
        destination: str | ResourceLocation,
        *,
        idempotency_key: str | None = None,
    ) -> Job:
        source_location = ResourceLocation.parse(source) if isinstance(source, str) else source
        destination_location = (
            ResourceLocation.parse(destination) if isinstance(destination, str) else destination
        )
        self._transfer(
            source_location.resource,
            Operation.DATA_READ,
            {
                "path": source_location.path,
                "destination": destination_location,
                "background": True,
            },
        )
        self._transfer(
            destination_location.resource,
            Operation.DATA_WRITE,
            {
                "path": destination_location.path,
                "source": source_location,
                "background": True,
            },
        )
        scopes = (
            JobScope(source_location.resource, Operation.DATA_READ),
            JobScope(destination_location.resource, Operation.DATA_WRITE),
        )
        self._reconcile_jobs()
        return self._job_manager().submit(
            JobKind.COPY,
            scopes,
            {
                "source": f"{source_location.resource}:{source_location.path}",
                "destination": f"{destination_location.resource}:{destination_location.path}",
            },
            idempotency_key=idempotency_key,
            lock_token=self._lock_token,
            local_only=self._local_scopes(scopes),
        )

    def list_jobs(self) -> tuple[Job, ...]:
        return tuple(job for job in self._job_manager().list() if self._job_allowed(job))

    def inspect_job(self, job_id: str) -> Job:
        job = self._job_manager().get(job_id)
        self._authorize_job(job)
        return job

    def read_job_log(
        self,
        job_id: str,
        stream: str,
        *,
        offset: int = 0,
        limit: int = 64 * 1024,
    ) -> JobLog:
        if stream not in {"stdout", "stderr"}:
            raise ValueError("stream must be 'stdout' or 'stderr'")
        job = self.inspect_job(job_id)
        del job
        return self._job_manager().read_log(
            job_id,
            cast(Literal["stdout", "stderr"], stream),
            offset=offset,
            limit=limit,
        )

    def cancel_job(self, job_id: str) -> Job:
        job = self.inspect_job(job_id)
        del job
        return self._job_manager().cancel(job_id)

    def _job_manager(self) -> JobManager:
        if self._jobs is None:
            raise JobsUnavailableError("durable jobs require a service loaded from configuration")
        return self._jobs

    def _job_allowed(self, job: Job) -> bool:
        return all(
            self._authorization.allows(scope.resource, scope.operation) for scope in job.scopes
        )

    def _authorize_job(self, job: Job) -> None:
        if self._job_allowed(job):
            return
        raise AuthorizationDeniedError(f"authorization denied for job {job.id!r}")

    def _compute(
        self,
        resource: str,
        operation: Operation,
        context: Mapping[str, object],
    ) -> ComputeCapability:
        target = self._registry.get(resource)
        capability = target.capabilities.compute
        if capability is None:
            self._unsupported(target, operation)
        self._authorize(resource, operation, context)
        return capability

    def _data(
        self,
        resource: str,
        operation: Operation,
        context: Mapping[str, object],
    ) -> ResourceCapabilities:
        target = self._registry.get(resource)
        if target.capabilities.addressing is None:
            self._unsupported(target, operation)
        self._authorize(resource, operation, context)
        return target.capabilities

    def _transfer(
        self,
        resource: str,
        operation: Operation,
        context: Mapping[str, object],
    ) -> None:
        target = self._registry.get(resource)
        if target.capabilities.transfer is None:
            raise UnsupportedOperationError(f"resource {resource!r} does not support streamed copy")
        self._authorize(resource, operation, context)

    def _authorize(
        self,
        resource: str,
        operation: Operation,
        context: Mapping[str, object],
    ) -> None:
        self._authorization.authorize(AuthorizationRequest.create(resource, operation, context))

    def _with_permissions(self, inspection: ResourceInspection) -> ResourceInspection:
        allowed = tuple(
            operation
            for operation in inspection.supported_operations
            if self._authorization.allows(inspection.name, operation)
        )
        background = (
            tuple(operation for operation in allowed if operation.supports_background)
            if self._jobs is not None
            else ()
        )
        return replace(
            inspection,
            allowed_operations=allowed,
            background_operations=background,
        )

    @staticmethod
    def _validate_timeout(timeout_seconds: float | None) -> None:
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative or None")

    @staticmethod
    def _unsupported(resource: Resource, operation: Operation) -> Never:
        raise UnsupportedOperationError(
            f"resource {resource.name!r} does not support {operation.value}"
        )
