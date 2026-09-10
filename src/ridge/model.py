from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeAlias

PropertyScalar: TypeAlias = str | int | float | bool | None
PropertySource: TypeAlias = Literal["configured", "detected"]
OperationEffect: TypeAlias = Literal["read", "write", "execute"]


class Operation(StrEnum):
    COMPUTE_EXEC = "compute.exec"
    DATA_LIST = "data.list"
    DATA_READ = "data.read"
    DATA_WRITE = "data.write"
    DATA_STAT = "data.stat"
    DATA_DELETE = "data.delete"

    @property
    def effect(self) -> OperationEffect:
        if self is Operation.COMPUTE_EXEC:
            return "execute"
        if self in {Operation.DATA_WRITE, Operation.DATA_DELETE}:
            return "write"
        return "read"

    @property
    def idempotent(self) -> bool:
        return self not in {Operation.COMPUTE_EXEC, Operation.DATA_DELETE}

    @property
    def supports_background(self) -> bool:
        return self in {
            Operation.COMPUTE_EXEC,
            Operation.DATA_WRITE,
            Operation.DATA_DELETE,
        }


@dataclass(frozen=True, slots=True)
class ResourceProperty:
    value: PropertyScalar
    source: PropertySource


@dataclass(frozen=True, slots=True)
class ResourceInspection:
    name: str
    provider: str
    addressing: Literal["filesystem", "object"] | None
    supports_copy: bool
    supported_operations: tuple[Operation, ...]
    allowed_operations: tuple[Operation, ...]
    background_operations: tuple[Operation, ...]
    properties: dict[str, ResourceProperty]


@dataclass(frozen=True, slots=True)
class ExecResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float


FileKind: TypeAlias = Literal["file", "directory", "symlink", "other"]


@dataclass(frozen=True, slots=True)
class FileStat:
    path: str
    kind: FileKind
    size: int
    modified_ns: int


@dataclass(frozen=True, slots=True)
class ListEntry:
    path: str
    kind: FileKind
    size: int


@dataclass(frozen=True, slots=True)
class ResourceLocation:
    resource: str
    path: str

    @classmethod
    def parse(cls, value: str) -> ResourceLocation:
        resource, separator, path = value.partition(":")
        if not separator or not resource or not path:
            raise ValueError("resource locations must have the form RESOURCE:PATH")
        return cls(resource=resource, path=path)


@dataclass(frozen=True, slots=True)
class CopyRequest:
    source: ResourceLocation
    destination: ResourceLocation


@dataclass(frozen=True, slots=True)
class CopyResult:
    bytes_copied: int
    entries_copied: int
    duration_seconds: float


class JobStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"


class JobKind(StrEnum):
    EXECUTE = "execute"
    WRITE = "write"
    COPY = "copy"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class DeleteResult:
    """Bounded outcome; acknowledged does not assert prior existence (object storage)."""

    outcome: Literal["deleted", "missing", "acknowledged"]


@dataclass(frozen=True, slots=True)
class JobScope:
    resource: str
    operation: Operation


@dataclass(frozen=True, slots=True)
class LockRequest(JobScope):
    """Reserve an operation at a file/tree path, or the whole resource when omitted."""

    path: str | None = None

    def __post_init__(self) -> None:
        if self.path is not None and (not self.path or "\0" in self.path):
            raise ValueError("reservation path must be nonempty and contain no NUL bytes")


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    kind: JobKind
    status: JobStatus
    scopes: tuple[JobScope, ...]
    submitted_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None
    result: dict[str, object] | None
    cancellation_requested: bool
    access_scope_id: str | None = None


@dataclass(frozen=True, slots=True)
class JobSummary:
    id: str
    kind: JobKind
    status: JobStatus
    scopes: tuple[JobScope, ...]
    submitted_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class JobPage:
    jobs: tuple[JobSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class JobLog:
    job_id: str
    stream: Literal["stdout", "stderr"]
    offset: int
    next_offset: int
    complete: bool
    content: bytes


TransferPayloadKind: TypeAlias = Literal["file", "tree"]


@dataclass(frozen=True, slots=True)
class ObjectStat:
    key: str
    size: int
    etag: str
    modified_at: str | None


@dataclass(frozen=True, slots=True)
class ObjectEntry:
    key: str
    size: int
    etag: str
    modified_at: str | None


@dataclass(frozen=True, slots=True)
class ObjectPage:
    entries: tuple[ObjectEntry, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class DataPage:
    """One directory-children or object-prefix page; entry metadata keeps its semantics."""

    addressing: Literal["filesystem", "object"]
    entries: tuple[ListEntry, ...] | tuple[ObjectEntry, ...]
    next_cursor: str | None
