from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import BinaryIO, Literal, Protocol, runtime_checkable

from ridge.model import (
    DeleteResult,
    ExecResult,
    FileStat,
    ListEntry,
    ObjectPage,
    ObjectStat,
    Operation,
    ResourceProperty,
    TransferPayloadKind,
)


@runtime_checkable
class ComputeCapability(Protocol):
    def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecResult: ...


@runtime_checkable
class StreamingComputeCapability(Protocol):
    """Optional mechanism for compute backends that can stream job output."""

    def exec_streaming(
        self,
        argv: Sequence[str],
        stdout: BinaryIO,
        stderr: BinaryIO,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecResult: ...


@runtime_checkable
class FilesystemCapability(Protocol):
    def list(self, path: str = ".") -> tuple[ListEntry, ...]: ...

    def read(self, path: str, *, max_bytes: int | None = None) -> bytes: ...

    def write(
        self,
        path: str,
        content: bytes,
    ) -> None: ...

    def stat(self, path: str) -> FileStat: ...


class TransferSource(Protocol):
    kind: TransferPayloadKind

    def read(self, size: int) -> bytes: ...

    def finish(self) -> None: ...

    def cancel(self) -> None: ...


class TransferDestination(Protocol):
    def write(self, content: bytes) -> None: ...

    def finish(self) -> tuple[int, int]: ...

    def commit(self) -> None: ...

    def abort(self) -> None: ...

    def cancel(self) -> None: ...


@runtime_checkable
class TransferCapability(Protocol):
    def open_transfer_source(self, path: str) -> TransferSource: ...

    def open_transfer_destination(
        self, path: str, kind: TransferPayloadKind
    ) -> TransferDestination: ...


@runtime_checkable
class DeleteCapability(Protocol):
    """Optional exact-path/key deletion using the resource's data addressing model."""

    def delete(self, path: str, *, recursive: bool = False) -> DeleteResult: ...


@runtime_checkable
class StorageCapability(Protocol):
    def list_objects(
        self,
        prefix: str = "",
        *,
        cursor: str | None = None,
        limit: int = 1000,
    ) -> ObjectPage: ...

    def read_object(self, key: str, *, max_bytes: int | None = None) -> bytes: ...

    def write_object(self, key: str, content: bytes) -> None: ...

    def stat_object(self, key: str) -> ObjectStat: ...


def _validate_capability(name: str, implementation: object, contract: type[object]) -> None:
    if not isinstance(implementation, contract):
        raise TypeError(f"{name} capability does not implement its Ridge contract")


@dataclass(frozen=True, slots=True)
class ResourceCapabilities:
    """Typed capability implementations exposed by one named resource."""

    compute: ComputeCapability | None = None
    filesystem: FilesystemCapability | None = None
    storage: StorageCapability | None = None
    transfer: TransferCapability | None = None
    delete: DeleteCapability | None = None

    def __post_init__(self) -> None:
        if self.filesystem is not None and self.storage is not None:
            raise ValueError("a resource must select one data addressing model")
        if self.transfer is not None and self.filesystem is None and self.storage is None:
            raise ValueError("transfer requires a data capability")
        if self.delete is not None and self.filesystem is None and self.storage is None:
            raise ValueError("delete requires a data capability")
        contracts = (
            ("compute", self.compute, ComputeCapability),
            ("filesystem", self.filesystem, FilesystemCapability),
            ("storage", self.storage, StorageCapability),
            ("transfer", self.transfer, TransferCapability),
            ("delete", self.delete, DeleteCapability),
        )
        for name, implementation, contract in contracts:
            if implementation is not None:
                _validate_capability(name, implementation, contract)
        if self.compute is None and self.filesystem is None and self.storage is None:
            raise ValueError("a resource must expose compute, filesystem, or storage")

    @property
    def operations(self) -> tuple[Operation, ...]:
        operations: list[Operation] = []
        if self.compute is not None:
            operations.append(Operation.COMPUTE_EXEC)
        if self.filesystem is not None or self.storage is not None:
            operations.extend(
                (
                    Operation.DATA_LIST,
                    Operation.DATA_READ,
                    Operation.DATA_WRITE,
                    Operation.DATA_STAT,
                )
            )
        if self.delete is not None:
            operations.append(Operation.DATA_DELETE)
        return tuple(operations)

    @property
    def addressing(self) -> Literal["filesystem", "object"] | None:
        if self.filesystem is not None:
            return "filesystem"
        if self.storage is not None:
            return "object"
        return None


class Resource(Protocol):
    name: str
    provider_name: str
    capabilities: ResourceCapabilities

    def inspect_properties(self) -> Mapping[str, ResourceProperty]: ...
