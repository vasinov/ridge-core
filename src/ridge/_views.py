"""Lazy data views: provider resolution runs inside the caller's operation claim."""

from collections.abc import Mapping
from dataclasses import replace

from ridge.errors import UnsupportedOperationError
from ridge.model import (
    DeleteResult,
    FileStat,
    ListEntry,
    ObjectPage,
    ObjectStat,
    ResourceProperty,
    TransferPayloadKind,
)
from ridge.registry import ResourceRegistry
from ridge.resource import Resource, ResourceCapabilities, TransferDestination, TransferSource


class DataView:
    def __init__(self, resource: Resource, roots: tuple[str, ...]) -> None:
        self.name = resource.name
        self.provider_name = resource.provider_name
        self._resource = resource
        self._roots = roots
        original = resource.capabilities
        if original.data_views is None:
            raise UnsupportedOperationError(f"resource {self.name!r} does not support data views")
        self.capabilities = replace(
            original,
            filesystem=self if original.filesystem else None,
            storage=self if original.storage else None,
            transfer=self if original.transfer else None,
            delete=self if original.delete else None,
        )

    def inspect_properties(self) -> Mapping[str, ResourceProperty]:
        return self._resource.inspect_properties()

    def _open(self) -> ResourceCapabilities:
        provider = self._resource.capabilities.data_views
        assert provider is not None
        return provider.open_data_view(self._roots)

    def list(self, path: str = ".") -> tuple[ListEntry, ...]:
        target = self._open().filesystem
        assert target is not None
        return target.list(path)

    def read(self, path: str, *, max_bytes: int | None = None) -> bytes:
        target = self._open().filesystem
        assert target is not None
        return target.read(path, max_bytes=max_bytes)

    def write(self, path: str, content: bytes) -> None:
        target = self._open().filesystem
        assert target is not None
        target.write(path, content)

    def stat(self, path: str) -> FileStat:
        target = self._open().filesystem
        assert target is not None
        return target.stat(path)

    def list_objects(
        self, prefix: str = "", *, cursor: str | None = None, limit: int = 1000
    ) -> ObjectPage:
        target = self._open().storage
        assert target is not None
        return target.list_objects(prefix, cursor=cursor, limit=limit)

    def read_object(self, key: str, *, max_bytes: int | None = None) -> bytes:
        target = self._open().storage
        assert target is not None
        return target.read_object(key, max_bytes=max_bytes)

    def write_object(self, key: str, content: bytes) -> None:
        target = self._open().storage
        assert target is not None
        target.write_object(key, content)

    def stat_object(self, key: str) -> ObjectStat:
        target = self._open().storage
        assert target is not None
        return target.stat_object(key)

    def delete(self, path: str, *, recursive: bool = False) -> DeleteResult:
        target = self._open().delete
        assert target is not None
        return target.delete(path, recursive=recursive)

    def open_transfer_source(self, path: str) -> TransferSource:
        target = self._open().transfer
        assert target is not None
        return target.open_transfer_source(path)

    def open_transfer_destination(
        self, path: str, kind: TransferPayloadKind
    ) -> TransferDestination:
        target = self._open().transfer
        assert target is not None
        return target.open_transfer_destination(path, kind)


def bind_data_views(
    registry: ResourceRegistry, roots: Mapping[str, tuple[str, ...]]
) -> ResourceRegistry:
    return ResourceRegistry(
        DataView(registry.get(name), roots[name]) if roots.get(name) else registry.get(name)
        for name in registry.names()
    )
