"""Deletion contracts across addressing, authorization, frontends, and job admission."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal, cast
from unittest.mock import Mock

import anyio
import pytest
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from ridge import DeleteResult, JobScope, Operation, RidgeService
from ridge.authorization import AuthorizationPolicy
from ridge.backends._helper import HelperOperations, HelperTransportResult
from ridge.backends._source import HELPER_SOURCE
from ridge.backends.local import LocalResource
from ridge.backends.s3 import S3Resource
from ridge.cli import app
from ridge.conformance import check_delete_capability
from ridge.errors import (
    AuthorizationDeniedError,
    ExecutionError,
    InvalidPathError,
    JobConflictError,
    LockConflictError,
    LockOwnershipError,
    PathTypeError,
    ResourceUnavailableError,
    UnsupportedOperationError,
)
from ridge.model import JobStatus
from ridge.registry import ResourceRegistry
from ridge.resource import ResourceCapabilities


class _Helper:
    def __init__(self, root: Path) -> None:
        self.root = root

    def invoke_helper(
        self, operation: str, request_bytes: bytes, *, timeout_seconds: float | None
    ) -> HelperTransportResult:
        assert timeout_seconds is None
        result = subprocess.run(
            [sys.executable, "-I", "-c", HELPER_SOURCE, str(self.root), operation],
            input=request_bytes,
            capture_output=True,
            check=True,
            timeout=5,
        )
        return HelperTransportResult(result.stdout, result.stderr)


@pytest.fixture(params=["local", "helper"])
def deleter(request: pytest.FixtureRequest, tmp_path: Path) -> LocalResource | HelperOperations:
    return (
        LocalResource("local", tmp_path)
        if request.param == "local"
        else HelperOperations(_Helper(tmp_path))
    )


def test_files_empty_dirs_and_missing(
    deleter: LocalResource | HelperOperations, tmp_path: Path
) -> None:
    (tmp_path / "file").write_bytes(b"remove")
    (tmp_path / "empty").mkdir()
    (tmp_path / "neighbor").write_bytes(b"keep")
    for path in ("file", "empty"):
        assert deleter.delete(path) == DeleteResult("deleted")
        assert deleter.delete(path) == DeleteResult("missing")
    assert deleter.delete("absent/child") == DeleteResult("missing")
    assert (tmp_path / "neighbor").read_bytes() == b"keep"


def test_delete_conformance(tmp_path: Path) -> None:
    local = LocalResource("local", tmp_path)
    check_delete_capability(
        local, path="entry", write=local.write, exists=lambda path: (tmp_path / path).exists()
    )


def test_s3_delete_disables_sdk_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = Mock()
    monkeypatch.setattr("ridge.backends.s3.boto3.client", factory)
    resource = S3Resource("objects", bucket="bucket", region="us-west-2")
    assert resource.delete("entry").outcome == "acknowledged"
    assert factory.call_args.kwargs["config"].retries == {"total_max_attempts": 1}
    factory.return_value.delete_object.assert_called_once_with(Bucket="bucket", Key="entry")
    assert Operation.DATA_DELETE.effect == "write"
    assert Operation.DATA_DELETE.supports_background
    assert not Operation.DATA_DELETE.idempotent


def test_cancel_unstarted_delete_has_no_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RidgeService.from_config(_config(tmp_path))
    target = tmp_path / "data" / "entry"
    target.write_text("keep")
    with monkeypatch.context() as patch:
        patch.setattr("ridge._job_runner.subprocess.Popen", Mock(return_value=None))
        job = service.submit_delete("local", "entry")
    assert service.cancel_job(job.id).status is JobStatus.CANCELLED
    assert target.read_text() == "keep"


def test_delete_job_failure_and_policy_visibility(tmp_path: Path) -> None:
    config = _config(tmp_path)
    service = RidgeService.from_config(config)
    target = tmp_path / "data" / "tree"
    target.mkdir()
    (target / "entry").write_text("keep")
    job = service.submit_delete("local", "tree")
    assert _wait(service, job.id) is JobStatus.FAILED
    assert "recursive" in str(service.inspect_job(job.id).error)
    assert (target / "entry").read_text() == "keep"
    config.write_text(config.read_text().replace("[data.delete]", "[data.stat]"))
    restricted = RidgeService.from_config(config)
    assert restricted.list_jobs().jobs == ()
    with pytest.raises(AuthorizationDeniedError):
        restricted.inspect_job(job.id)


def test_recursion_and_links(deleter: LocalResource | HelperOperations, tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "file").write_bytes(b"remove")
    neighbor = tmp_path / "neighbor"
    neighbor.write_bytes(b"keep")
    (tree / "link").symlink_to(neighbor)
    (tree / "broken").symlink_to("missing")
    with pytest.raises(PathTypeError, match="recursive"):
        deleter.delete("tree")
    assert (tree / "file").read_bytes() == b"remove"
    assert deleter.delete("tree", recursive=True).outcome == "deleted"
    assert neighbor.read_bytes() == b"keep"
    for name, target in (
        ("outside", tmp_path.parent),
        ("broken", tmp_path / "missing"),
        ("root-link", tmp_path),
    ):
        (tmp_path / name).symlink_to(target)
        assert deleter.delete(name).outcome == "deleted"
        assert not (tmp_path / name).is_symlink()
    assert tmp_path.is_dir()


@pytest.mark.parametrize("path", [".", "./", "..", "dir/..", "../outside", "/", "a/../../outside"])
def test_root_and_escape_rejections(
    deleter: LocalResource | HelperOperations, tmp_path: Path, path: str
) -> None:
    with pytest.raises(InvalidPathError):
        deleter.delete(path, recursive=True)
    assert tmp_path.is_dir()


def test_parent_links_and_special_files(
    deleter: LocalResource | HelperOperations, tmp_path: Path
) -> None:
    (tmp_path / "outside").symlink_to(tmp_path.parent)
    with pytest.raises(InvalidPathError):
        deleter.delete("outside/anything", recursive=True)
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(PathTypeError, match="special"):
        deleter.delete("fifo")
    (tmp_path / "dir").mkdir()
    (tmp_path / "file").write_bytes(b"remove")
    assert deleter.delete("dir/../file").outcome == "deleted"


def _config(tmp_path: Path, grants: str = "data.delete") -> Path:
    (tmp_path / "data").mkdir(exist_ok=True)
    config = tmp_path / "ridge.yaml"
    config.write_text(
        "resources: {local: {provider: local, root: data}}\n"
        f"permissions: {{local: [{grants}]}}\nstate: {{directory: state}}\n"
    )
    return config


def _wait(service: RidgeService, identity: str) -> JobStatus:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = service.inspect_job(identity).status
        if status not in {JobStatus.STARTING, JobStatus.RUNNING}:
            return status
        time.sleep(0.02)
    raise AssertionError("delete job did not finish")


def test_authorization_precedes_effects(tmp_path: Path) -> None:
    resource = LocalResource("local", tmp_path)
    service = RidgeService(ResourceRegistry([resource]), AuthorizationPolicy.exact({}))
    spy = Mock(side_effect=AssertionError("must not dispatch"))
    resource.delete = spy
    with pytest.raises(AuthorizationDeniedError):
        service.delete_data("local", "target")
    with pytest.raises(AuthorizationDeniedError):
        service.submit_delete("local", "target")
    spy.assert_not_called()
    resource.capabilities = ResourceCapabilities(filesystem=resource)
    assert Operation.DATA_DELETE not in resource.capabilities.operations
    with pytest.raises(UnsupportedOperationError):
        service.delete_data("local", "target")
    with pytest.raises(ValueError, match="requires a data"):
        ResourceCapabilities(delete=resource)


def test_background_results_deduplication_and_claims(tmp_path: Path) -> None:
    service = RidgeService.from_config(_config(tmp_path))
    (tmp_path / "data" / "file").write_bytes(b"remove")
    lock = service.acquire_locks([JobScope("local", Operation.DATA_DELETE)])
    token = str(lock["token"])
    with pytest.raises(LockConflictError):
        service.delete_data("local", "file")
    with pytest.raises(LockConflictError):
        service.submit_delete("local", "file")
    job = service.with_lock(token).submit_delete("local", "file", idempotency_key="delete-once")
    assert _wait(service, job.id) is JobStatus.SUCCEEDED
    assert service.inspect_job(job.id).result == {"outcome": "deleted"}
    assert job.scopes == (JobScope("local", Operation.DATA_DELETE),)
    (tmp_path / "data" / "file").write_bytes(b"new entry")
    assert (
        service.with_lock(token).submit_delete("local", "file", idempotency_key="delete-once").id
        == job.id
    )
    assert (tmp_path / "data" / "file").read_bytes() == b"new entry"
    with pytest.raises(JobConflictError):
        service.with_lock(token).submit_delete(
            "local", "file", recursive=True, idempotency_key="delete-once"
        )
    service.release_locks(token)
    assert service.delete_data("local", "file").outcome == "deleted"
    assert all(
        item["status"] in {"released", "completed"}
        for item in cast(list[dict[str, object]], service.list_locks()["entries"])
    )


def test_write_session_cannot_declare_delete(tmp_path: Path) -> None:
    service = RidgeService.from_config(_config(tmp_path, "data.write, data.delete"))
    lock = service.acquire_locks([JobScope("local", Operation.DATA_WRITE)])
    with pytest.raises(LockOwnershipError):
        service.with_lock(str(lock["token"])).delete_data("local", "missing")
    service.release_locks(str(lock["token"]))


@pytest.mark.parametrize("path", ["", "\x00", ".", "/", "dir/.."])
def test_invalid_request_before_admission(tmp_path: Path, path: str) -> None:
    service = RidgeService.from_config(_config(tmp_path))
    with pytest.raises((ValueError, InvalidPathError)):
        service.submit_delete("local", path)
    assert service.list_jobs().jobs == ()
    assert service.list_locks()["entries"] == []


def test_partial_failure_is_not_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = RidgeService.from_config(_config(tmp_path))
    tree = tmp_path / "data" / "tree"
    tree.mkdir()
    (tree / "file").write_bytes(b"remove")
    original = Path.rmdir

    def fail_final(path: Path) -> None:
        if path == tree:
            raise PermissionError("injected final removal failure")
        original(path)

    monkeypatch.setattr(Path, "rmdir", fail_final)
    with pytest.raises(ExecutionError) as error:
        service.delete_data("local", "tree", recursive=True)
    assert any("partial" in note for note in error.value.__notes__)
    assert tree.is_dir() and not (tree / "file").exists()


def test_s3_exact_native_delete_without_stat() -> None:
    client = Mock()
    resource = S3Resource("objects", bucket="bucket", prefix="scope", client=client)
    service = RidgeService(
        ResourceRegistry([resource]),
        AuthorizationPolicy.exact({"objects": frozenset({Operation.DATA_DELETE})}),
    )
    for key in ("a/../b", "a//b", "prefix/", "missing"):
        assert service.delete_data("objects", key) == DeleteResult("acknowledged")
        client.delete_object.assert_called_with(Bucket="bucket", Key=f"scope/{key}")
    client.head_object.assert_not_called()
    client.list_objects_v2.assert_not_called()
    with pytest.raises(ValueError, match="recursive"):
        service.submit_delete("objects", "prefix", recursive=True)
    assert client.delete_object.call_count == 4


def test_cli_delete_and_background(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tree = tmp_path / "data" / "tree"
    tree.mkdir()
    (tree / "file").write_bytes(b"remove")
    runner = CliRunner()
    args = ["--config", str(config), "delete", "local", "tree"]
    assert runner.invoke(app, args).exit_code == 2
    rejected = runner.invoke(app, args + ["--idempotency-key", "invalid"])
    assert rejected.exit_code == 2 and (tree / "file").exists()
    submitted = runner.invoke(app, args + ["--recursive", "--background"])
    assert submitted.exit_code == 0, submitted.output
    identity = submitted.output.strip().split()[1]
    assert _wait(RidgeService.from_config(config), identity) is JobStatus.SUCCEEDED
    missing = runner.invoke(app, args)
    assert json.loads(missing.output) == {"outcome": "missing"}


@pytest.fixture
def anyio_backend() -> Literal["asyncio"]:
    return "asyncio"


@pytest.mark.anyio
async def test_stdio_delete_and_reconnect(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (tmp_path / "data" / "file").write_bytes(b"remove")
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "ridge.mcp", "--config", str(config)]
    )
    async with Client(params) as client:
        result = await client.call_tool(
            "delete_data",
            {
                "resource": "local",
                "path": "file",
                "background": True,
                "idempotency_key": "mcp-delete",
            },
        )
        assert not result.is_error and result.structured_content is not None
        identity = str(cast(dict[str, object], result.structured_content["job"])["id"])
    async with Client(params) as client:
        with anyio.fail_after(10):
            while True:
                result = await client.call_tool("inspect_job", {"job_id": identity})
                assert result.structured_content is not None
                if result.structured_content["status"] == "succeeded":
                    assert result.structured_content["result"] == {"outcome": "deleted"}
                    break
                await anyio.sleep(0.02)
        result = await client.call_tool("delete_data", {"resource": "local", "path": "file"})
        assert result.structured_content == {
            "mode": "completed",
            "result": {"outcome": "missing"},
            "job": None,
        }


def test_remote_failure_preserves_uncertain_claim(tmp_path: Path) -> None:
    config = _config(tmp_path)
    resource = LocalResource("local", tmp_path / "data")
    resource.provider_name = "remote-fixture"
    resource.delete = Mock(side_effect=ResourceUnavailableError("transport lost"))
    from ridge.jobs import JobManager

    service = RidgeService(
        ResourceRegistry([resource]), jobs=JobManager(tmp_path / "state", config, "fixture")
    )
    with pytest.raises(ResourceUnavailableError):
        service.delete_data("local", "file")
    entries = cast(list[dict[str, object]], service.list_locks()["entries"])
    assert len(entries) == 1 and entries[0]["status"] == "uncertain"
    with pytest.raises(LockConflictError):
        service.delete_data("local", "file")
