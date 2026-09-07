import base64
import json
import sys
from pathlib import Path

import pytest

from ridge.application import RidgeService
from ridge.authorization import AuthorizationPolicy
from ridge.backends.local import LocalResource
from ridge.errors import AuthorizationDeniedError
from ridge.model import ListEntry, Operation, TransferPayloadKind
from ridge.registry import ResourceRegistry
from ridge.resource import TransferSource


def test_service_routes_capabilities_and_copy(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_root.mkdir()
    destination_root.mkdir()
    service = RidgeService(
        ResourceRegistry(
            [
                LocalResource("source", source_root),
                LocalResource("destination", destination_root),
            ]
        ),
        AuthorizationPolicy.exact(
            {
                "source": frozenset(Operation),
                "destination": frozenset({Operation.DATA_READ, Operation.DATA_WRITE}),
            }
        ),
    )

    service.write_data("source", "input.txt", b"ridge")
    result = service.copy("source:input.txt", "destination:nested/output.txt")
    execution = service.execute(
        "source",
        [sys.executable, "-c", "print('ok')"],
        timeout_seconds=5,
    )

    assert service.read_data("destination", "nested/output.txt") == b"ridge"
    assert result.bytes_copied == 5
    assert execution.stdout == b"ok\n"
    with pytest.raises(AuthorizationDeniedError, match="compute.exec"):
        service.execute("destination", ["ignored"])


def test_service_reports_available_background_operations_with_canonical_names(
    tmp_path: Path,
) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {local: {provider: local, root: .}}")

    configured = RidgeService.from_config(config).inspect_resource("local")
    unavailable = RidgeService(
        ResourceRegistry([LocalResource("local", tmp_path)])
    ).inspect_resource("local")
    config.write_text(
        "resources: {local: {provider: local, root: .}}\n"
        "permissions: {local: [compute.exec, data.read]}\n"
    )
    restricted = RidgeService.from_config(config).inspect_resource("local")

    assert configured.background_operations == (
        Operation.COMPUTE_EXEC,
        Operation.DATA_WRITE,
    )
    assert Operation.DATA_READ not in configured.background_operations
    assert unavailable.background_operations == ()
    assert restricted.background_operations == (Operation.COMPUTE_EXEC,)


def test_service_rejects_negative_execution_timeout_before_invocation(tmp_path: Path) -> None:
    service = RidgeService(ResourceRegistry([LocalResource("local", tmp_path)]))

    with pytest.raises(ValueError, match="timeout_seconds must be non-negative or None"):
        service.execute("local", ["ignored"], timeout_seconds=-1)


def test_data_listing_pages_immediate_children_and_scopes_cursors(tmp_path: Path) -> None:
    resource = LocalResource("local", tmp_path)
    resource.write("a", b"a")
    resource.write("b", b"bb")
    resource.write("nested/c", b"ccc")
    service = RidgeService(ResourceRegistry([resource, LocalResource("other", tmp_path)]))
    first = service.list_data("local", limit=2)
    second = service.list_data("local", cursor=first.next_cursor, limit=2)
    assert first.addressing == second.addressing == "filesystem"
    assert [entry.path for entry in first.entries if isinstance(entry, ListEntry)] == ["a", "b"]
    assert [entry.path for entry in second.entries if isinstance(entry, ListEntry)] == ["nested"]
    assert second.next_cursor is None
    for name, path in [("other", None), ("local", "nested")]:
        with pytest.raises(ValueError, match="invalid cursor"):
            service.list_data(name, path, cursor=first.next_cursor)


@pytest.mark.parametrize("value", [None, {}, [], ["local", ".", -1], ["local", ".", True]])
def test_data_listing_rejects_malformed_cursor(tmp_path: Path, value: object) -> None:
    service = RidgeService(ResourceRegistry([LocalResource("local", tmp_path)]))
    cursor = base64.b64encode(json.dumps(value).encode()).decode()
    with pytest.raises(ValueError, match="invalid cursor"):
        service.list_data("local", cursor=cursor)


@pytest.mark.parametrize("limit", [0, -1, 1001, True])
def test_data_listing_validates_limits(tmp_path: Path, limit: int) -> None:
    service = RidgeService(ResourceRegistry([LocalResource("local", tmp_path)]))
    with pytest.raises(ValueError, match="limit"):
        service.list_data("local", limit=limit)


def test_copy_uses_bounded_streams_not_buffered_data_methods(tmp_path: Path) -> None:
    reads: list[int] = []

    class ObservedSource:
        def __init__(self, source: TransferSource) -> None:
            self.source = source
            self.kind: TransferPayloadKind = source.kind

        def read(self, size: int) -> bytes:
            reads.append(size)
            assert 0 < size <= 64 * 1024
            return self.source.read(size)

        def finish(self) -> None:
            self.source.finish()

        def cancel(self) -> None:
            self.source.cancel()

    class StreamingOnlyLocal(LocalResource):
        def read(self, path: str, *, max_bytes: int | None = None) -> bytes:
            raise AssertionError("copy must not use buffered reads")

        def write(self, path: str, content: bytes) -> None:
            raise AssertionError("copy must not use buffered writes")

        def open_transfer_source(self, path: str) -> TransferSource:
            return ObservedSource(super().open_transfer_source(path))

    payload = b"ridge\x00" * 200_000
    (tmp_path / "source").write_bytes(payload)
    service = RidgeService(ResourceRegistry([StreamingOnlyLocal("local", tmp_path)]))
    result = service.copy("local:source", "local:destination")
    assert result.bytes_copied == len(payload)
    assert (tmp_path / "destination").read_bytes() == payload
    assert len(reads) > 2
