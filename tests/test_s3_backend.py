from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError  # pyright: ignore[reportMissingTypeStubs]

from ridge.application import RidgeService
from ridge.backends.local import LocalResource
from ridge.backends.s3 import S3Resource
from ridge.conformance import check_storage_capability
from ridge.errors import (
    ObjectNotFoundError,
    OutputLimitExceededError,
    StorageError,
    UnsupportedOperationError,
)
from ridge.model import CopyRequest, ObjectEntry, ObjectStat, Operation, ResourceLocation
from ridge.registry import ResourceRegistry
from ridge.transfer import copy


class _MemoryS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.uploads: dict[str, tuple[str, str, dict[int, bytes]]] = {}
        self.aborted: list[str] = []
        self._next_upload = 1

    @staticmethod
    def _etag(content: bytes) -> str:
        return f'"{len(content):x}-{sum(content) % 65521:x}"'

    @staticmethod
    def _missing(operation: str) -> ClientError:
        return ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "missing"}},
            operation,
        )

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict[str, object]:
        content = bytes(Body)
        self.objects[(Bucket, Key)] = content
        return {"ETag": self._etag(content)}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        try:
            content = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise self._missing("GetObject") from exc
        return {
            "Body": io.BytesIO(content),
            "ContentLength": len(content),
            "ETag": self._etag(content),
        }

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        try:
            content = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise self._missing("HeadObject") from exc
        return {
            "ContentLength": len(content),
            "ETag": self._etag(content),
            "LastModified": datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
        }

    def list_objects_v2(self, **arguments: object) -> dict[str, object]:
        bucket = str(arguments["Bucket"])
        prefix = str(arguments["Prefix"])
        maximum = int(str(arguments["MaxKeys"]))
        start = int(str(arguments.get("ContinuationToken", "0")))
        keys = sorted(
            key
            for candidate_bucket, key in self.objects
            if candidate_bucket == bucket and key.startswith(prefix)
        )
        page = keys[start : start + maximum]
        next_index = start + len(page)
        return {
            "Contents": [
                {
                    "Key": key,
                    "Size": len(self.objects[(bucket, key)]),
                    "ETag": self._etag(self.objects[(bucket, key)]),
                    "LastModified": datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
                }
                for key in page
            ],
            "IsTruncated": next_index < len(keys),
            "NextContinuationToken": str(next_index),
        }

    def create_multipart_upload(self, *, Bucket: str, Key: str) -> dict[str, object]:
        upload_id = str(self._next_upload)
        self._next_upload += 1
        self.uploads[upload_id] = (Bucket, Key, {})
        return {"UploadId": upload_id}

    def upload_part(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        PartNumber: int,
        Body: bytes,
    ) -> dict[str, object]:
        upload_bucket, upload_key, parts = self.uploads[UploadId]
        assert (Bucket, Key) == (upload_bucket, upload_key)
        parts[PartNumber] = bytes(Body)
        return {"ETag": self._etag(bytes(Body))}

    def complete_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        MultipartUpload: dict[str, Any],
    ) -> dict[str, object]:
        upload_bucket, upload_key, parts = self.uploads.pop(UploadId)
        assert (Bucket, Key) == (upload_bucket, upload_key)
        numbers = [int(part["PartNumber"]) for part in MultipartUpload["Parts"]]
        content = b"".join(parts[number] for number in numbers)
        self.objects[(Bucket, Key)] = content
        return {"ETag": self._etag(content)}

    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> dict[str, object]:
        upload_bucket, upload_key, _ = self.uploads.pop(UploadId)
        assert (Bucket, Key) == (upload_bucket, upload_key)
        self.aborted.append(UploadId)
        return {}


def _resource(client: _MemoryS3Client) -> S3Resource:
    return S3Resource(
        "artifacts",
        bucket="ridge-fixture",
        prefix="runs/current",
        region="us-west-2",
        client=client,
    )


def test_storage_operations_apply_prefix_replace_and_paginate() -> None:
    client = _MemoryS3Client()
    resource = _resource(client)
    resource.write_object("a.txt", b"first")
    resource.write_object("a.txt", b"second")
    resource.write_object("nested/b.txt", b"bravo")
    client.put_object(Bucket="ridge-fixture", Key="runs/currently/outside", Body=b"outside")

    first = resource.list_objects(limit=1)
    second = resource.list_objects(cursor=first.next_cursor, limit=1)

    assert resource.read_object("a.txt") == b"second"
    assert resource.stat_object("a.txt").key == "a.txt"
    assert [entry.key for entry in first.entries] == ["a.txt"]
    assert [entry.key for entry in second.entries] == ["nested/b.txt"]
    assert first.next_cursor == "1"
    assert second.next_cursor is None
    inspection = ResourceRegistry([resource]).inspect("artifacts")
    assert inspection.properties["region"].value == "us-west-2"


def test_reusable_storage_conformance() -> None:
    check_storage_capability(_resource(_MemoryS3Client()))


def test_unified_data_keeps_exact_keys_prefixes_and_backend_pagination() -> None:
    client = _MemoryS3Client()
    resource = _resource(client)
    service = RidgeService(ResourceRegistry([resource]))
    for key in [".", "a/../b", "a//b", "b"]:
        service.write_data("artifacts", key, key.encode())
    service.write_data("artifacts", "b", b"replacement")
    first = service.list_data("artifacts", "a/", limit=1)
    second = service.list_data("artifacts", "a/", cursor=first.next_cursor, limit=1)
    assert first.addressing == second.addressing == "object"
    assert [entry.key for entry in first.entries if isinstance(entry, ObjectEntry)] == ["a/../b"]
    assert [entry.key for entry in second.entries if isinstance(entry, ObjectEntry)] == ["a//b"]
    assert second.next_cursor is None
    assert service.read_data("artifacts", "a/../b") == b"a/../b"
    assert service.read_data("artifacts", "b") == b"replacement"
    stat = service.stat_data("artifacts", ".")
    assert isinstance(stat, ObjectStat) and stat.key == "."
    inspection = service.inspect_resource("artifacts")
    assert inspection.provider == "s3" and inspection.addressing == "object"
    assert inspection.supports_copy
    assert inspection.supported_operations == tuple(
        op for op in Operation if op != Operation.COMPUTE_EXEC
    )


def test_storage_read_bounds_and_missing_objects() -> None:
    client = _MemoryS3Client()
    resource = _resource(client)
    resource.write_object("large.bin", b"1234")

    with pytest.raises(OutputLimitExceededError):
        resource.read_object("large.bin", max_bytes=3)
    with pytest.raises(ObjectNotFoundError):
        resource.stat_object("missing")


@pytest.mark.parametrize("maximum", [None, 0, 4])
@pytest.mark.parametrize("size", [0, 4, 5, 131072])
def test_storage_read_bounds_body_despite_stale_head(
    monkeypatch: pytest.MonkeyPatch, maximum: int | None, size: int
) -> None:
    client = _MemoryS3Client()
    resource = _resource(client)
    resource.write_object("changing", b"")
    reads: list[int] = []
    consumed = 0

    class ShortBody(io.BytesIO):
        def read(self, size: int | None = -1, /) -> bytes:
            nonlocal consumed
            assert size is not None
            reads.append(size)
            if maximum is not None:
                assert 0 < size <= maximum + 1 - consumed
            chunk = super().read(min(size, 2) if size >= 0 else size)
            consumed += len(chunk)
            return chunk

    body = ShortBody(b"x" * size)

    def get_object(**kwargs: object) -> dict[str, object]:
        return {"Body": body}

    monkeypatch.setattr(client, "get_object", get_object)
    if maximum is not None and size > maximum:
        with pytest.raises(OutputLimitExceededError):
            resource.read_object("changing", max_bytes=maximum)
        assert consumed == maximum + 1
    else:
        assert resource.read_object("changing", max_bytes=maximum) == b"x" * size
        assert consumed == size
    assert body.closed
    assert reads


def test_copy_replaces_files_between_filesystem_and_s3(tmp_path: Path) -> None:
    client = _MemoryS3Client()
    storage = _resource(client)
    local = LocalResource("local", tmp_path)
    local.write("input.bin", b"ridge\x00")
    storage.write_object("artifact.bin", b"old")
    registry = ResourceRegistry([local, storage])

    outbound = copy(
        registry,
        CopyRequest(
            ResourceLocation("local", "input.bin"), ResourceLocation("artifacts", "artifact.bin")
        ),
    )
    local.write("output.bin", b"stale")
    inbound = copy(
        registry,
        CopyRequest(
            ResourceLocation("artifacts", "artifact.bin"), ResourceLocation("local", "output.bin")
        ),
    )

    assert storage.read_object("artifact.bin") == b"ridge\x00"
    assert local.read("output.bin") == b"ridge\x00"
    assert outbound.bytes_copied == inbound.bytes_copied == 6


def test_s3_keys_are_not_posix_normalized_for_same_location_detection() -> None:
    client = _MemoryS3Client()
    storage = _resource(client)
    storage.write_object("a/../source", b"ridge")
    registry = ResourceRegistry([storage])

    copy(
        registry,
        CopyRequest(
            ResourceLocation("artifacts", "a/../source"),
            ResourceLocation("artifacts", "source"),
        ),
    )

    assert storage.read_object("source") == b"ridge"


def test_copy_supports_empty_objects_and_multipart_streams(tmp_path: Path) -> None:
    client = _MemoryS3Client()
    storage = _resource(client)
    local = LocalResource("local", tmp_path)
    local.write("empty", b"")
    large = b"ridge" * (2 * 1024 * 1024)
    local.write("large", large)
    registry = ResourceRegistry([local, storage])

    copy(
        registry,
        CopyRequest(ResourceLocation("local", "empty"), ResourceLocation("artifacts", "empty")),
    )
    copy(
        registry,
        CopyRequest(ResourceLocation("local", "large"), ResourceLocation("artifacts", "large")),
    )

    assert storage.read_object("empty") == b""
    assert storage.read_object("large") == large
    assert client.uploads == {}


def test_copy_rejects_tree_to_s3_without_creating_an_object(tmp_path: Path) -> None:
    client = _MemoryS3Client()
    storage = _resource(client)
    local = LocalResource("local", tmp_path)
    local.write("tree/file", b"ridge")
    registry = ResourceRegistry([local, storage])

    with pytest.raises(UnsupportedOperationError, match="filesystem tree"):
        copy(
            registry,
            CopyRequest(ResourceLocation("local", "tree"), ResourceLocation("artifacts", "tree")),
        )

    assert client.objects == {}


def test_failed_multipart_copy_is_aborted(tmp_path: Path) -> None:
    class FailingClient(_MemoryS3Client):
        def upload_part(self, **arguments: Any) -> dict[str, object]:
            del arguments
            raise ClientError(
                {"Error": {"Code": "InternalError", "Message": "fixture failure"}},
                "UploadPart",
            )

    client = FailingClient()
    storage = _resource(client)
    local = LocalResource("local", tmp_path)
    local.write("large", b"x" * (9 * 1024 * 1024))
    registry = ResourceRegistry([local, storage])

    with pytest.raises(StorageError, match="fixture failure"):
        copy(
            registry,
            CopyRequest(ResourceLocation("local", "large"), ResourceLocation("artifacts", "large")),
        )

    assert client.uploads == {}
    assert client.aborted == ["1"]
