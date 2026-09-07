from __future__ import annotations

from pathlib import Path

import pytest

from ridge.application import RidgeService
from ridge.authorization import AuthorizationPolicy, AuthorizationRequest
from ridge.backends.local import LocalResource
from ridge.backends.s3 import S3Resource
from ridge.backends.ssh import SshResource
from ridge.errors import AuthorizationDeniedError
from ridge.model import Operation, TransferPayloadKind
from ridge.registry import ResourceRegistry
from ridge.resource import TransferDestination, TransferSource


def _policy(**grants: set[Operation]) -> AuthorizationPolicy:
    return AuthorizationPolicy.exact(
        {resource: frozenset(operations) for resource, operations in grants.items()}
    )


def test_inspection_exposes_supported_and_allowed_operations(tmp_path: Path) -> None:
    service = RidgeService(
        ResourceRegistry([LocalResource("data", tmp_path)]),
        _policy(data={Operation.DATA_READ, Operation.DATA_STAT}),
    )

    inspection = service.inspect_resource("data")

    assert Operation.DATA_WRITE in inspection.supported_operations
    assert inspection.allowed_operations == (
        Operation.DATA_READ,
        Operation.DATA_STAT,
    )
    assert inspection.properties


def test_denial_occurs_before_capability_invocation_and_preserves_context(
    tmp_path: Path,
) -> None:
    class RecordingPolicy:
        def __init__(self) -> None:
            self.requests: list[AuthorizationRequest] = []

        def allows(self, resource: str, operation: Operation) -> bool:
            del resource, operation
            return False

        def authorize(self, request: AuthorizationRequest) -> None:
            self.requests.append(request)
            raise AuthorizationDeniedError("denied by fixture")

    policy = RecordingPolicy()
    service = RidgeService(ResourceRegistry([LocalResource("data", tmp_path)]), policy)

    with pytest.raises(AuthorizationDeniedError, match="denied by fixture"):
        service.write_data("data", "nested/output.txt", b"should not exist")

    assert not (tmp_path / "nested").exists()
    assert policy.requests == [
        AuthorizationRequest.create("data", Operation.DATA_WRITE, {"path": "nested/output.txt"})
    ]


def test_copy_authorizes_both_endpoints_before_opening_either(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_root.mkdir()
    destination_root.mkdir()
    (source_root / "input.txt").write_text("ridge")
    service = RidgeService(
        ResourceRegistry(
            [
                LocalResource("source", source_root),
                LocalResource("destination", destination_root),
            ]
        ),
        _policy(source={Operation.DATA_READ}),
    )

    with pytest.raises(AuthorizationDeniedError, match="data.write"):
        service.copy("source:input.txt", "destination:nested/output.txt")

    assert not (destination_root / "nested").exists()


def test_s3_denial_precedes_client_access() -> None:
    class FailingClient:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"S3 client must not be accessed: {name}")

    service = RidgeService(
        ResourceRegistry([S3Resource("artifacts", bucket="fixture", client=FailingClient())]),
        _policy(artifacts={Operation.DATA_LIST}),
    )

    with pytest.raises(AuthorizationDeniedError, match="data.write"):
        service.write_data("artifacts", "denied.txt", b"no")


def test_ssh_denial_precedes_transport_access() -> None:
    service = RidgeService(
        ResourceRegistry(
            [
                SshResource(
                    "remote",
                    host="unavailable.invalid",
                    root="/tmp",
                    python_executable="python3",
                    ssh_executable="must-not-run",
                )
            ]
        ),
        _policy(remote=set()),
    )

    with pytest.raises(AuthorizationDeniedError, match="compute.exec"):
        service.execute("remote", ["true"])


@pytest.mark.parametrize(
    "method,args,operation",
    [
        ("list_data", ("data", "."), Operation.DATA_LIST),
        ("read_data", ("data", "file"), Operation.DATA_READ),
        ("write_data", ("data", "file", b"content"), Operation.DATA_WRITE),
        ("stat_data", ("data", "file"), Operation.DATA_STAT),
    ],
)
def test_exact_policy_default_denies_each_ungranted_operation(
    tmp_path: Path,
    method: str,
    args: tuple[object, ...],
    operation: Operation,
) -> None:
    service = RidgeService(
        ResourceRegistry([LocalResource("data", tmp_path)]),
        _policy(data=set()),
    )

    with pytest.raises(AuthorizationDeniedError, match=operation.value):
        getattr(service, method)(*args)


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("source_allowed", [False, True])
def test_copy_denies_before_opening_either_endpoint(
    tmp_path: Path,
    background: bool,
    source_allowed: bool,
) -> None:
    class UnopenedResource(LocalResource):
        def open_transfer_source(self, path: str) -> TransferSource:
            raise AssertionError("source opened before both grants")

        def open_transfer_destination(
            self,
            path: str,
            kind: TransferPayloadKind,
        ) -> TransferDestination:
            raise AssertionError("destination opened before both grants")

    service = RidgeService(
        ResourceRegistry(
            [UnopenedResource("source", tmp_path), UnopenedResource("dest", tmp_path)]
        ),
        _policy(
            source={Operation.DATA_READ} if source_allowed else set(),
            dest=set() if source_allowed else {Operation.DATA_WRITE},
        ),
    )
    operation = service.submit_copy if background else service.copy
    with pytest.raises(AuthorizationDeniedError):
        operation("source:missing", "dest:new/output")
    assert not (tmp_path / "new").exists()
