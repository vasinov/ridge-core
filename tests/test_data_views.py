import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from ridge import AccessGrant, JobScope, Operation, RidgeService
from ridge._access import ScopeAccessError, ScopeStore
from ridge.backends._source import HELPER_SOURCE, TRANSFER_HELPER_SOURCE
from ridge.backends.local import LocalResource
from ridge.backends.s3 import S3Resource
from ridge.config import load_configuration
from ridge.errors import (
    AuthorizationDeniedError,
    InvalidPathError,
    LockConflictError,
    PathNotFoundError,
)

READ = Operation.DATA_READ
WRITE = Operation.DATA_WRITE


def workspace(tmp_path: Path) -> tuple[Path, RidgeService]:
    (tmp_path / "data").mkdir()
    path = tmp_path / "ridge.yaml"
    path.write_text(
        "resources:\n  data: {provider: local, root: data}\ndelegation:\n  data: [data.read, data.write, data.stat, data.list, data.delete, compute.exec]\n"
    )
    return path, RidgeService.from_config(path)


def test_view_creation_is_lazy_and_operations_keep_canonical_claims(tmp_path: Path) -> None:
    path, operator = workspace(tmp_path)
    grant = operator.create_scope([AccessGrant("data", frozenset({READ, WRITE}), data_root="new")])
    child = RidgeService.from_config(path, scope_token=grant.token)
    assert not (tmp_path / "data/new").exists()
    held = operator.acquire_locks([JobScope("data", WRITE)])
    with pytest.raises(LockConflictError):
        child.write_data("data", "value", b"no")
    operator.release_locks(str(held["token"]))
    with pytest.raises(PathNotFoundError):
        child.write_data("data", "value", b"no")
    assert not (tmp_path / "data/new").exists()
    (tmp_path / "data/new").mkdir()
    child.write_data("data", "value", b"yes")
    assert operator.read_data("data", "new/value") == b"yes"
    with pytest.raises(InvalidPathError):
        child.read_data("data", "../outside")


def test_each_ancestor_root_is_enforced_and_compute_is_not_rebased(tmp_path: Path) -> None:
    path, operator = workspace(tmp_path)
    (tmp_path / "data/parent").mkdir()
    (tmp_path / "data/outside").mkdir()
    (tmp_path / "data/parent/link").symlink_to("../outside", target_is_directory=True)
    operations = frozenset({READ, WRITE, Operation.COMPUTE_EXEC})
    parent = operator.create_scope([AccessGrant("data", operations, operations, "parent")])
    controller = RidgeService.from_config(path, scope_token=parent.token)
    issued = controller.create_scope([AccessGrant("data", operations, data_root="link")])
    child = RidgeService.from_config(path, scope_token=issued.token)
    with pytest.raises(InvalidPathError, match="parent view"):
        child.write_data("data", "no", b"no")
    assert not (tmp_path / "data/outside/no").exists()
    result = child.execute("data", [sys.executable, "-c", "import os; print(os.getcwd())"])
    assert result.stdout.strip().decode() == str(tmp_path / "data")


def test_root_resolution_runs_under_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, operator = workspace(tmp_path)
    (tmp_path / "data/view").mkdir()
    scope = operator.create_scope([AccessGrant("data", frozenset({WRITE}), data_root="view")])
    child = RidgeService.from_config(path, scope_token=scope.token)
    original = LocalResource.open_data_view

    def inspect(self: LocalResource, roots: tuple[str, ...]):
        assert operator.list_locks()["entries"]
        return original(self, roots)

    monkeypatch.setattr(LocalResource, "open_data_view", inspect)
    child.write_data("data", "value", b"yes")


def test_view_validation_holds_no_transaction_and_rechecks_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, operator = workspace(tmp_path)
    parent = operator.create_scope([AccessGrant("data", frozenset(), frozenset({WRITE}))])
    controller = RidgeService.from_config(path, scope_token=parent.token)
    original = LocalResource.validate_data_root

    def close(self: LocalResource, root: str) -> None:
        operator.revoke_scope(parent.scope.id)
        original(self, root)

    monkeypatch.setattr(LocalResource, "validate_data_root", close)
    with pytest.raises(ScopeAccessError, match="revoked"):
        controller.create_scope([AccessGrant("data", frozenset({WRITE}), data_root="child")])


@pytest.mark.parametrize("root", ["", "../x", "/absolute", "parent/../other", "bad\0root"])
def test_invalid_root_is_rejected_at_issue(tmp_path: Path, root: str) -> None:
    _path, operator = workspace(tmp_path)
    with pytest.raises((ValueError, InvalidPathError)):
        operator.create_scope([AccessGrant("data", frozenset({WRITE}), data_root=root)])


def test_omitted_root_inherits_view_and_survives_reconnect(tmp_path: Path) -> None:
    path, operator = workspace(tmp_path)
    (tmp_path / "data/parent").mkdir()
    parent = operator.create_scope([AccessGrant("data", frozenset(), frozenset({WRITE}), "parent")])
    controller = RidgeService.from_config(path, scope_token=parent.token)
    scope = controller.create_scope([AccessGrant("data", frozenset({WRITE}))])
    for value in (b"first", b"second"):
        RidgeService.from_config(path, scope_token=scope.token).write_data("data", "value", value)
    assert (tmp_path / "data/parent/value").read_bytes() == b"second"
    assert not (tmp_path / "data/value").exists()


def test_s3_view_preserves_literal_keys_without_connecting() -> None:
    client = Mock()
    resource = S3Resource("objects", bucket="fixture", prefix="base", client=client)
    view = resource.open_data_view(("parent//literal", "../child"))
    client.assert_not_called()
    assert not client.method_calls
    assert view.storage is not None and view.delete is not None
    view.storage.write_object("../key//value", b"payload")
    client.put_object.assert_called_once_with(
        Bucket="fixture", Key="base/parent//literal/../child/../key//value", Body=b"payload"
    )
    view.delete.delete("../key//value")
    client.delete_object.assert_called_once_with(
        Bucket="fixture", Key="base/parent//literal/../child/../key//value"
    )


def test_provider_without_views_rejects_narrowing(tmp_path: Path) -> None:
    path, _operator = workspace(tmp_path)
    loaded = load_configuration(path)
    resource = loaded.registry.get("data")
    assert isinstance(resource, LocalResource)
    resource.capabilities = replace(resource.capabilities, data_views=None)
    with pytest.raises(ScopeAccessError, match="unsupported_data_view"):
        ScopeStore(tmp_path / ".ridge").issue(
            loaded, [AccessGrant("data", frozenset({READ}), data_root="child")]
        )


def test_delegate_only_parent_supervises_but_cannot_use_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, operator = workspace(tmp_path)
    monkeypatch.setattr("ridge.jobs.subprocess.Popen", Mock())
    parent = operator.create_scope([AccessGrant("data", frozenset(), frozenset({WRITE}))])
    controller = RidgeService.from_config(path, scope_token=parent.token)
    scope = controller.create_scope([AccessGrant("data", frozenset({WRITE}))])
    child = RidgeService.from_config(path, scope_token=scope.token)
    job = child.submit_write("data", "value", b"yes")
    assert controller.inspect_job(job.id).id == job.id
    assert [item.id for item in controller.list_jobs().jobs] == [job.id]
    assert controller.read_job_log(job.id, "stdout").content == b""
    with pytest.raises(AuthorizationDeniedError):
        controller.write_data("data", "no", b"no")
    original = path.read_text()
    path.write_text(original.replace("data.write, ", ""))
    with pytest.raises(AuthorizationDeniedError):
        controller.inspect_job(job.id)
    assert controller.list_jobs().jobs == ()
    path.write_text(original)
    controller.revoke_scope(scope.scope.id)
    assert controller.cancel_job(job.id).status.value == "cancelled"


@pytest.mark.parametrize(
    "source,operation,payload",
    [
        (HELPER_SOURCE, "write", b'{"path":"no"}\npayload'),
        (HELPER_SOURCE, "delete", b'{"path":"no","recursive":false}\n'),
        (TRANSFER_HELPER_SOURCE, "stage-file", b'{"path":"no"}\n'),
    ],
)
def test_remote_helpers_check_all_root_boundaries(
    tmp_path: Path, source: str, operation: str, payload: bytes
) -> None:
    (tmp_path / "parent").mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "parent/link").symlink_to("../outside", target_is_directory=True)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            source,
            str(tmp_path),
            operation,
            json.dumps(["parent", "link"]),
        ],
        input=payload,
        capture_output=True,
        timeout=5,
        check=False,
    )
    response = json.loads(result.stdout or result.stderr)
    assert response["error"] == "invalid_path"
    assert list((tmp_path / "outside").iterdir()) == []
