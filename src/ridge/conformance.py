"""Reusable, destructive checks for provider capability implementations."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ridge.errors import OutputLimitExceededError
from ridge.resource import (
    ComputeCapability,
    FilesystemCapability,
    StorageCapability,
    TransferCapability,
)

_FIRST = b"\x00ridge-first\xff"
_REPLACEMENT = b"\x00ridge-replacement\xff"


def check_filesystem_capability(
    capability: FilesystemCapability,
    *,
    directory: str = "ridge-conformance",
) -> None:
    """Check binary replacement, bounded read, stat, and list in a disposable directory."""
    path = f"{directory.rstrip('/')}/data.bin"
    capability.write(path, _FIRST)
    capability.write(path, _REPLACEMENT)
    assert capability.read(path) == _REPLACEMENT, "filesystem replacement did not round-trip"
    stat = capability.stat(path)
    assert stat.path == path and stat.kind == "file" and stat.size == len(_REPLACEMENT)
    entries = capability.list(directory)
    assert any(entry.path == path and entry.size == len(_REPLACEMENT) for entry in entries)
    try:
        capability.read(path, max_bytes=1)
    except OutputLimitExceededError:
        pass
    else:
        raise AssertionError("filesystem bounded read did not reject oversized content")


def check_storage_capability(
    capability: StorageCapability,
    *,
    prefix: str = "ridge-conformance",
) -> None:
    """Check binary replacement, bounded read, stat, and pagination in a disposable prefix."""
    base = prefix.rstrip("/")
    first_key = f"{base}/a.bin"
    second_key = f"{base}/b.bin"
    capability.write_object(first_key, _FIRST)
    capability.write_object(first_key, _REPLACEMENT)
    capability.write_object(second_key, b"second")
    assert capability.read_object(first_key) == _REPLACEMENT, (
        "storage replacement did not round-trip"
    )
    stat = capability.stat_object(first_key)
    assert stat.key == first_key and stat.size == len(_REPLACEMENT)
    first_page = capability.list_objects(f"{base}/", limit=1)
    assert [entry.key for entry in first_page.entries] == [first_key]
    assert first_page.next_cursor is not None, "storage pagination did not return a cursor"
    second_page = capability.list_objects(f"{base}/", cursor=first_page.next_cursor, limit=1)
    assert [entry.key for entry in second_page.entries] == [second_key]
    try:
        capability.read_object(first_key, max_bytes=1)
    except OutputLimitExceededError:
        pass
    else:
        raise AssertionError("storage bounded read did not reject oversized content")


def check_compute_capability(
    capability: ComputeCapability,
    argv: Sequence[str],
    *,
    expected_stdout: bytes,
    cwd: str | None = None,
) -> None:
    """Check exact argv execution using a caller-supplied portable command."""
    result = capability.exec(argv, cwd=cwd, timeout_seconds=10)
    assert result.argv == tuple(argv), "compute result did not preserve argv"
    assert result.exit_code == 0, f"compute command exited with {result.exit_code}"
    assert result.stdout == expected_stdout, "compute stdout did not match"


def check_file_transfer_capability(
    source: TransferCapability,
    destination: TransferCapability,
    *,
    source_path: str,
    destination_path: str,
    expected_content: bytes,
    read_destination: Callable[[str], bytes],
) -> None:
    """Check a single-file transfer between disposable provider namespaces."""
    source_stream = source.open_transfer_source(source_path)
    destination_stream = None
    try:
        assert source_stream.kind == "file", "transfer source did not identify a file"
        destination_stream = destination.open_transfer_destination(destination_path, "file")
        while content := source_stream.read(64 * 1024):
            destination_stream.write(content)
        source_stream.finish()
        bytes_copied, entries_copied = destination_stream.finish()
        destination_stream.commit()
    except BaseException:
        source_stream.cancel()
        if destination_stream is not None:
            destination_stream.cancel()
            destination_stream.abort()
        raise
    assert bytes_copied == len(expected_content) and entries_copied == 1
    assert read_destination(destination_path) == expected_content
