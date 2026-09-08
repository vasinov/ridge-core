"""Backend-neutral cross-resource copy orchestration."""

from __future__ import annotations

import posixpath
import time

from ridge.errors import InvalidPathError, UnsupportedOperationError, format_error
from ridge.model import CopyRequest, CopyResult, ResourceLocation
from ridge.registry import ResourceRegistry
from ridge.resource import (
    Resource,
    TransferCapability,
    TransferDestination,
    TransferSource,
)

_CHUNK_SIZE = 64 * 1024


def _transfer_capability(resource: Resource) -> TransferCapability:
    capability = resource.capabilities.transfer
    if capability is None:
        raise UnsupportedOperationError(
            f"resource {resource.name!r} does not support cross-resource transfer"
        )
    return capability


def _same_location(
    source: ResourceLocation,
    destination: ResourceLocation,
    resource: Resource,
) -> bool:
    if source.resource != destination.resource:
        return False
    if resource.capabilities.storage is not None:
        return source.path == destination.path
    return posixpath.normpath(source.path) == posixpath.normpath(destination.path)


def copy(registry: ResourceRegistry, request: CopyRequest) -> CopyResult:
    raw_source_resource = registry.get(request.source.resource)
    if _same_location(request.source, request.destination, raw_source_resource):
        raise InvalidPathError("copy source and destination must be different locations")

    source_resource = _transfer_capability(raw_source_resource)
    destination_resource = _transfer_capability(registry.get(request.destination.resource))
    started = time.monotonic()
    source: TransferSource | None = None
    destination: TransferDestination | None = None
    destination_finished = False
    try:
        source = source_resource.open_transfer_source(request.source.path)
        destination = destination_resource.open_transfer_destination(
            request.destination.path, source.kind
        )
        first_chunk = source.read(_CHUNK_SIZE)
        destination.write(first_chunk)
        while chunk := source.read(_CHUNK_SIZE):
            destination.write(chunk)

        source_error: Exception | None = None
        try:
            source.finish()
        except Exception as exc:  # noqa: BLE001 - preserve the source failure through cleanup
            source_error = exc

        destination_error: Exception | None = None
        try:
            bytes_copied, entries_copied = destination.finish()
            destination_finished = True
        except Exception as exc:  # noqa: BLE001 - compare both endpoint outcomes
            bytes_copied = entries_copied = 0
            destination_error = exc
        if source_error is not None:
            if destination_error is not None:
                source_error.add_note(
                    f"destination staging also failed: {format_error(destination_error)}"
                )
            raise source_error
        if destination_error is not None:
            raise destination_error
        destination.commit()
        return CopyResult(
            bytes_copied=bytes_copied,
            entries_copied=entries_copied,
            duration_seconds=time.monotonic() - started,
        )
    except BaseException as error:
        if source is not None:
            try:
                source.cancel()
            except Exception as cleanup_error:  # noqa: BLE001 - preserve primary failure
                error.add_note(f"source cleanup also failed: {format_error(cleanup_error)}")
        if destination is not None:
            if not destination_finished:
                try:
                    destination.cancel()
                except Exception as cleanup_error:  # noqa: BLE001 - preserve primary failure
                    error.add_note(
                        f"destination cancellation also failed: {format_error(cleanup_error)}"
                    )
            try:
                destination.abort()
            except Exception as cleanup_error:  # noqa: BLE001 - do not mask the primary failure
                error.add_note(f"destination cleanup also failed: {format_error(cleanup_error)}")
        raise
