from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

import boto3  # pyright: ignore[reportMissingTypeStubs]
from botocore.exceptions import (  # pyright: ignore[reportMissingTypeStubs]
    BotoCoreError,
    ClientError,
)

from ridge.errors import (
    InvalidPathError,
    ObjectNotFoundError,
    OutputLimitExceededError,
    RidgeError,
    StorageError,
    TransferError,
    UnsupportedOperationError,
)
from ridge.model import (
    ObjectEntry,
    ObjectPage,
    ObjectStat,
    PropertyScalar,
    ResourceProperty,
    TransferPayloadKind,
)
from ridge.resource import ResourceCapabilities, TransferDestination, TransferSource

_PART_SIZE = 8 * 1024 * 1024


def _modified_at(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _etag(value: object) -> str:
    return str(value or "").strip('"')


def _storage_failure(action: str, error: BaseException) -> RidgeError:
    if isinstance(error, ClientError):
        response = cast(dict[str, Any], error.response)  # pyright: ignore[reportUnknownMemberType]
        detail = cast(dict[str, Any], response.get("Error", {}))
        code = str(detail.get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return ObjectNotFoundError(f"object does not exist during {action}")
        message = str(detail.get("Message") or code or error)
        return StorageError(f"S3 {action} failed: {message}")
    return StorageError(f"S3 {action} failed: {error}")


class _S3TransferSource(TransferSource):
    kind: TransferPayloadKind = "file"

    def __init__(self, resource: S3Resource, key: str) -> None:
        self._resource = resource
        self._key = key
        try:
            response = resource.client.get_object(Bucket=resource.bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise _storage_failure("read", exc) from exc
        self._stream = response["Body"]
        self._size = int(response["ContentLength"])
        self._etag = _etag(response.get("ETag"))
        self._bytes_read = 0
        self._closed = False

    def read(self, size: int) -> bytes:
        try:
            content = cast(bytes, self._stream.read(size))
        except (BotoCoreError, OSError) as exc:
            raise _storage_failure("read", exc) from exc
        self._bytes_read += len(content)
        return content

    def finish(self) -> None:
        self._close()
        if self._bytes_read != self._size:
            raise TransferError(
                f"S3 object changed or ended early: expected {self._size} bytes, "
                f"received {self._bytes_read}"
            )
        current = self._resource._stat_full_key(  # pyright: ignore[reportPrivateUsage]
            self._key
        )
        if current.size != self._size or current.etag != self._etag:
            raise TransferError("S3 object changed during transfer")

    def cancel(self) -> None:
        self._close()

    def _close(self) -> None:
        if not self._closed:
            self._stream.close()
            self._closed = True


class _S3TransferDestination(TransferDestination):
    def __init__(self, resource: S3Resource, key: str) -> None:
        self._resource = resource
        self._key = key
        self._buffer = bytearray()
        self._upload_id: str | None = None
        self._parts: list[dict[str, object]] = []
        self._bytes = 0
        self._finished = False

    def write(self, content: bytes) -> None:
        if self._finished:
            raise TransferError("transfer destination is already closed")
        self._buffer.extend(content)
        self._bytes += len(content)
        if self._upload_id is None and len(self._buffer) > _PART_SIZE:
            self._start_multipart()
        while self._upload_id is not None and len(self._buffer) >= _PART_SIZE:
            self._upload_part(bytes(self._buffer[:_PART_SIZE]))
            del self._buffer[:_PART_SIZE]

    def finish(self) -> tuple[int, int]:
        if self._finished:
            raise TransferError("transfer destination is already closed")
        if self._upload_id is not None and self._buffer:
            self._upload_part(bytes(self._buffer))
            self._buffer.clear()
        self._finished = True
        return self._bytes, 1

    def commit(self) -> None:
        if not self._finished:
            raise TransferError("transfer destination has not finished staging")
        try:
            if self._upload_id is None:
                self._resource.client.put_object(
                    Bucket=self._resource.bucket,
                    Key=self._key,
                    Body=bytes(self._buffer),
                )
                self._buffer.clear()
            else:
                upload_id = self._upload_id
                self._resource.client.complete_multipart_upload(
                    Bucket=self._resource.bucket,
                    Key=self._key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": self._parts},
                )
                self._upload_id = None
        except (BotoCoreError, ClientError) as exc:
            raise _storage_failure("write", exc) from exc

    def abort(self) -> None:
        if self._upload_id is None:
            self._buffer.clear()
            return
        upload_id = self._upload_id
        try:
            self._resource.client.abort_multipart_upload(
                Bucket=self._resource.bucket,
                Key=self._key,
                UploadId=upload_id,
            )
        except (BotoCoreError, ClientError) as exc:
            raise _storage_failure("abort multipart upload", exc) from exc
        self._upload_id = None

    def cancel(self) -> None:
        self.abort()

    def _start_multipart(self) -> None:
        try:
            response = self._resource.client.create_multipart_upload(
                Bucket=self._resource.bucket,
                Key=self._key,
            )
        except (BotoCoreError, ClientError) as exc:
            raise _storage_failure("start multipart upload", exc) from exc
        self._upload_id = str(response["UploadId"])

    def _upload_part(self, content: bytes) -> None:
        if self._upload_id is None:
            raise AssertionError("multipart upload has not started")
        part_number = len(self._parts) + 1
        if part_number > 10_000:
            raise TransferError("S3 multipart upload exceeds 10,000 parts")
        try:
            response = self._resource.client.upload_part(
                Bucket=self._resource.bucket,
                Key=self._key,
                UploadId=self._upload_id,
                PartNumber=part_number,
                Body=content,
            )
        except (BotoCoreError, ClientError) as exc:
            raise _storage_failure("upload part", exc) from exc
        self._parts.append({"ETag": response["ETag"], "PartNumber": part_number})


class S3Resource:
    provider_name = "s3"

    def __init__(
        self,
        name: str,
        *,
        bucket: str,
        prefix: str | None = None,
        region: str | None = None,
        configured_properties: Mapping[str, PropertyScalar] | None = None,
        client: Any | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("S3 bucket must not be empty")
        if prefix is not None and prefix.startswith("/"):
            raise InvalidPathError("S3 prefix must not begin with '/'")
        self.name = name
        self.bucket = bucket
        self.prefix = (prefix or "").rstrip("/")
        self.region = region
        self._configured_properties = dict(configured_properties or {})
        self._client: Any = client
        self.capabilities = ResourceCapabilities(storage=self, transfer=self)

    @property
    def client(self) -> Any:
        if self._client is None:
            client_factory = cast(Any, boto3.client)  # pyright: ignore[reportUnknownMemberType]
            self._client = client_factory("s3", region_name=self.region)
        return self._client

    def inspect_properties(self) -> Mapping[str, ResourceProperty]:
        configured: dict[str, PropertyScalar] = {
            **self._configured_properties,
            "bucket": self.bucket,
        }
        if self.prefix:
            configured["prefix"] = self.prefix
        if self.region is not None:
            configured["region"] = self.region
        return {
            key: ResourceProperty(value=value, source="configured")
            for key, value in configured.items()
        }

    def list_objects(
        self,
        prefix: str = "",
        *,
        cursor: str | None = None,
        limit: int = 1000,
    ) -> ObjectPage:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        arguments: dict[str, object] = {
            "Bucket": self.bucket,
            "Prefix": self._full_key(prefix, allow_empty=True),
            "MaxKeys": limit,
        }
        if cursor is not None:
            arguments["ContinuationToken"] = cursor
        try:
            response = self.client.list_objects_v2(**arguments)
        except (BotoCoreError, ClientError) as exc:
            raise _storage_failure("list", exc) from exc
        entries = tuple(
            ObjectEntry(
                key=self._relative_key(str(item["Key"])),
                size=int(item["Size"]),
                etag=_etag(item.get("ETag")),
                modified_at=_modified_at(item.get("LastModified")),
            )
            for item in response.get("Contents", ())
        )
        next_cursor = (
            str(response["NextContinuationToken"])
            if response.get("IsTruncated") and response.get("NextContinuationToken")
            else None
        )
        return ObjectPage(entries=entries, next_cursor=next_cursor)

    def read_object(self, key: str, *, max_bytes: int | None = None) -> bytes:
        full_key = self._full_key(key)
        if max_bytes is not None:
            if max_bytes < 0:
                raise ValueError("max_bytes must be non-negative or None")
            size = self._stat_full_key(full_key).size
            if size > max_bytes:
                raise OutputLimitExceededError(
                    f"object is {size} bytes, exceeding the {max_bytes}-byte limit: {key}"
                )
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=full_key)
            body = response["Body"]
            try:
                if max_bytes is None:
                    return cast(bytes, body.read())
                content = bytearray()
                while len(content) <= max_bytes:
                    chunk = cast(bytes, body.read(max_bytes + 1 - len(content)))
                    if not chunk:
                        return bytes(content)
                    content.extend(chunk)
                raise OutputLimitExceededError(f"object exceeds the {max_bytes}-byte limit: {key}")
            finally:
                body.close()
        except (BotoCoreError, ClientError) as exc:
            raise _storage_failure("read", exc) from exc

    def write_object(self, key: str, content: bytes) -> None:
        try:
            self.client.put_object(Bucket=self.bucket, Key=self._full_key(key), Body=content)
        except (BotoCoreError, ClientError) as exc:
            raise _storage_failure("write", exc) from exc

    def stat_object(self, key: str) -> ObjectStat:
        return self._stat_full_key(self._full_key(key))

    def open_transfer_source(self, path: str) -> TransferSource:
        return _S3TransferSource(self, self._full_key(path))

    def open_transfer_destination(
        self, path: str, kind: TransferPayloadKind
    ) -> TransferDestination:
        if kind != "file":
            raise UnsupportedOperationError("S3 cannot represent a filesystem tree")
        return _S3TransferDestination(self, self._full_key(path))

    def _stat_full_key(self, full_key: str) -> ObjectStat:
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=full_key)
        except (BotoCoreError, ClientError) as exc:
            raise _storage_failure("stat", exc) from exc
        return ObjectStat(
            key=self._relative_key(full_key),
            size=int(response["ContentLength"]),
            etag=_etag(response.get("ETag")),
            modified_at=_modified_at(response.get("LastModified")),
        )

    def _full_key(self, key: str, *, allow_empty: bool = False) -> str:
        if key.startswith("/"):
            raise InvalidPathError("S3 keys must be relative to the configured prefix")
        if not key and not allow_empty:
            raise InvalidPathError("S3 object key must not be empty")
        if self.prefix:
            return f"{self.prefix}/{key}" if key else f"{self.prefix}/"
        return key

    def _relative_key(self, key: str) -> str:
        marker = f"{self.prefix}/" if self.prefix else ""
        return key.removeprefix(marker)
