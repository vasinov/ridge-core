"""Protect real filesystem effects with deterministic overlapping calls."""

# pyright: reportPrivateUsage=false
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Literal
from unittest.mock import Mock

import pytest
from mcp import Client
from typer.testing import CliRunner

from ridge import (
    AccessGrant,
    LockRequest,
    ManagedMCPSession,
    Operation,
    RidgeService,
    ScopeAccessError,
)
from ridge._job_process import current_job
from ridge._planning import FootprintPlanner
from ridge.backends._footprints import FilesystemFootprints
from ridge.backends._scripts.footprints import validate_filesystem_footprint
from ridge.backends.docker import DockerResource
from ridge.backends.local import _RootedFilesystem
from ridge.backends.ssh import SshResource
from ridge.claims import Claim
from ridge.cli import app
from ridge.errors import LockConflictError, LockOwnershipError
from ridge.mcp import create_server
from ridge.model import JobScope
from ridge.registry import ResourceRegistry

READ, WRITE, STAT = Operation.DATA_READ, Operation.DATA_WRITE, Operation.DATA_STAT
Workspace = tuple[Path, RidgeService]


@pytest.fixture
def anyio_backend() -> Literal["asyncio"]:
    return "asyncio"


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, RidgeService]:
    root = tmp_path / "data"
    root.mkdir()
    (root / "dir").mkdir()
    (root / "dir/a").write_bytes(b"initial")
    path = tmp_path / "ridge.yaml"
    path.write_text(
        "resources:\n"
        "  a: {provider: local, root: data, lock_key: files}\n"
        "  alias: {provider: local, root: data, lock_key: files}\n"
        "delegation: {a: [data.read, data.write, data.stat]}\n"
    )
    return root, RidgeService.from_config(path)


def test_publication_overlaps_sibling_but_blocks_alias_directory_and_staging(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, service = workspace
    original = _RootedFilesystem.write

    def write(filesystem: _RootedFilesystem, path: str, content: bytes) -> None:
        if path == "dir/a":
            service.write_data("a", "dir/b", b"sibling")
            for resource, target in (("alias", "dir/a"), ("a", "dir/./a"), ("a", "dir/A")):
                with pytest.raises(LockConflictError):
                    service.write_data(resource, target, b"conflict")
            with pytest.raises(LockConflictError):
                service.list_data("a", "dir")
            with pytest.raises(LockConflictError):
                service.stat_data("a", "dir")
            with pytest.raises(LockConflictError):
                service.delete_data("a", "dir", recursive=True)
            with pytest.raises(LockConflictError):
                service.read_data("a", "dir/.ridge-write-anything")
        return original(filesystem, path, content)

    monkeypatch.setattr(_RootedFilesystem, "write", write)
    service.write_data("a", "dir/a", b"published")
    assert (root / "dir/a").read_bytes() == b"published"
    assert (root / "dir/b").read_bytes() == b"sibling"
    assert sorted(p.name for p in (root / "dir").iterdir()) == ["a", "b"]
    assert service.list_locks()["entries"] == []


def test_managed_read_modify_write_and_reconnect(workspace: Workspace) -> None:
    root, service = workspace
    requests = [LockRequest("a", op, "dir/a") for op in (READ, WRITE, STAT)]
    with service.lock_session(requests, lease_seconds=2) as session:
        content = session.service.read_data("a", "dir/a")
        service.write_data("alias", "dir/b", b"neighbor")
        with pytest.raises(LockConflictError):
            service.write_data("alias", "dir/a", b"lost update")
        with pytest.raises(LockOwnershipError, match="exceed"):
            session.service.write_data("a", "dir/b", b"out of reservation")
        session.service.write_data("a", "dir/a", content + b" updated")
        assert session.service.stat_data("a", "dir/a").size == 15
    token = str(service.acquire_locks(requests)["token"])
    reconnect = RidgeService.from_config(root.parent / "ridge.yaml").with_lock(token)
    assert reconnect.read_data("a", "dir/a") == b"initial updated"
    reconnect.release_locks(token)


@pytest.mark.parametrize("case", ["symlink", "parents", "hardlink", "dotdot", "staging"])
def test_fallback_preserves_behavior_but_never_enlarges_reservation(
    workspace: Workspace, case: str
) -> None:
    root, service = workspace
    if case == "symlink":
        (root / "link").symlink_to("dir", target_is_directory=True)
        path = "link/a"
    elif case == "hardlink":
        (root / "other").hardlink_to(root / "dir/a")
        path = "other"
    else:
        path = {
            "parents": "new/child",
            "dotdot": "dir/../other",
            "staging": "dir/.ridge-write-test",
        }[case]
    with pytest.raises(LockOwnershipError, match="reservation"):
        service.acquire_locks([LockRequest("a", WRITE, path)])
    assert service.list_locks()["entries"] == []
    service.write_data("a", path, b"fallback")
    assert (root / path).read_bytes() == b"fallback"


def test_tree_reservation_copy_delete_and_shared_input_set(workspace: Workspace) -> None:
    root, service = workspace
    (root / "source").mkdir()
    (root / "source/in").write_bytes(b"source")
    requests = [LockRequest("a", op, "dir") for op in (WRITE, Operation.DATA_DELETE)]
    requests.append(LockRequest("a", READ, "source"))
    with service.lock_session(requests) as session:
        session.service.copy("a:source", "a:dir/result")
        with pytest.raises(LockConflictError):
            service.read_data("alias", "dir/a")
        session.service.delete_data("a", "dir/result", recursive=True)
        service.read_data("alias", "source/in")
        with pytest.raises(LockConflictError):
            service.write_data("alias", "source/in", b"changed")
    assert (root / "dir/a").read_bytes() == b"initial"
    assert not (root / "dir/result").exists()


def test_scoped_coordinates_and_reservation_authority(workspace: Workspace) -> None:
    root, service = workspace
    issued = service.create_scope([AccessGrant("a", frozenset({READ, WRITE}), data_root="dir")])
    child = RidgeService.from_config(root.parent / "ridge.yaml", scope_token=issued.token)
    token = str(child.acquire_locks([LockRequest("a", WRITE, "a")])["token"])
    child.with_lock(token).write_data("a", "a", b"child")
    service.write_data("alias", "dir/b", b"neighbor")
    with pytest.raises(LockConflictError):
        service.write_data("alias", "dir/a", b"collision")
    service.revoke_scope(issued.scope.id)
    with pytest.raises(ScopeAccessError):
        child.with_lock(token).write_data("a", "a", b"revoked")
    service.release_locks(token)


def test_background_persists_narrow_claims_replay_and_revalidates(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, service = workspace
    monkeypatch.setattr("ridge.jobs.subprocess.Popen", Mock())
    first = service.submit_write("a", "dir/a", b"new", idempotency_key="same")
    assert service.submit_write("a", "dir/a", b"new", idempotency_key="same").id == first.id
    service.submit_write("alias", "dir/b", b"neighbor")
    with pytest.raises(LockConflictError):
        service.write_data("alias", "dir/a", b"collision")
    assert service.inspect_lock(first.id)["claims"] == [
        {"domain": "files", "scope": ["filesystem", "dir", "a"], "mode": "exclusive"}
    ]
    # Simulate a changed physical mapping before worker dispatch.
    (root / "outside").write_bytes(b"unchanged")
    (root / "dir/a").unlink()
    (root / "dir/a").symlink_to("../outside")
    token = current_job.set(first.id)
    try:
        with pytest.raises(LockOwnershipError):
            service.write_data("a", "dir/a", b"must not dispatch")
    finally:
        current_job.reset(token)
    assert (root / "outside").read_bytes() == b"unchanged"


def test_cli_path_reservations(workspace: Workspace) -> None:
    root, service = workspace
    runner = CliRunner()
    result = runner.invoke(
        app, ["--config", str(root.parent / "ridge.yaml"), "locks", "acquire", "a:data.write:dir/a"]
    )
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    service.write_data("alias", "dir/b", b"neighbor")
    with pytest.raises(LockConflictError):
        service.write_data("alias", "dir/a", b"blocked")
    service.release_locks(value["token"])


@pytest.mark.anyio
async def test_mcp_managed_path_reservations(workspace: Workspace) -> None:
    _root, service = workspace
    async with (
        Client(create_server(service)) as client,
        ManagedMCPSession(
            client, [LockRequest("a", op, "dir/a") for op in (WRITE, Operation.DATA_DELETE)]
        ) as session,
    ):
        service.write_data("alias", "dir/b", b"neighbor")
        with pytest.raises(LockConflictError):
            service.write_data("alias", "dir/a", b"blocked")
        result = await session.call_tool(
            "write_data", {"resource": "a", "path": "dir/a", "content": "updated"}
        )
        assert not result.is_error
        deleted = await session.call_tool("delete_data", {"resource": "a", "path": "dir/a"})
        assert not deleted.is_error


def test_validation_failure_releases_without_dispatch(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, service = workspace
    monkeypatch.setattr(
        FilesystemFootprints, "validate_footprint", Mock(side_effect=OSError("probe failed"))
    )
    with pytest.raises(OSError, match="probe failed"):
        service.write_data("a", "dir/a", b"not dispatched")
    assert (root / "dir/a").read_bytes() == b"initial"
    assert service.list_locks()["entries"] == []


def test_tree_validation_bounds_and_nonportable_names(workspace: Workspace) -> None:
    root, service = workspace
    for index in range(4097):
        (root / "dir" / str(index)).touch()
    assert not validate_filesystem_footprint(root, ("dir",))
    for path in ("dir", "résultat", "has space", "trailing."):
        with pytest.raises(LockOwnershipError):
            service.acquire_locks([LockRequest("a", READ, path)])
    service.write_data("a", "résultat", b"supported with broad protection")
    assert service.read_data("a", "résultat") == b"supported with broad protection"


def test_linux_mount_alias_validation(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _service = workspace
    read_text = Path.read_text
    mount = root / "dir"
    mounts = ["1 0 1:1 / / rw - ext4 /dev/fixture rw\n"]

    def read(path: Path, *args: object, **kwargs: object) -> str:
        if str(path) == "/proc/self/mountinfo":
            return "".join(mounts)
        return read_text(path)

    monkeypatch.setattr("ridge.backends._scripts.footprints.sys.platform", "linux")
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(
        "ridge.backends._scripts.footprints.os.readlink", Mock(return_value=str(root))
    )
    assert validate_filesystem_footprint(root, ("dir/a",))
    mounts.append(f"2 1 1:1 / {mount} rw - ext4 /dev/fixture rw\n")
    assert not validate_filesystem_footprint(root, ("dir/a",))
    assert not validate_filesystem_footprint(root, (".",))
    assert not validate_filesystem_footprint(root / ".." / root.name, ("dir/a",))


def test_path_reservation_releases_after_revocation_during_validation(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, service = workspace
    issued = service.create_scope([AccessGrant("a", frozenset({WRITE}), data_root="dir")])
    child = RidgeService.from_config(root.parent / "ridge.yaml", scope_token=issued.token)

    def validate(_guard: FilesystemFootprints, _path: str, _roots: tuple[str, ...]) -> bool:
        service.revoke_scope(issued.scope.id)
        return True

    monkeypatch.setattr(FilesystemFootprints, "validate_footprint", validate)
    with pytest.raises(ScopeAccessError):
        child.acquire_locks([LockRequest("a", WRITE, "a")])
    assert service.list_locks()["entries"] == []


def test_concurrent_idempotent_preparation_returns_one_job(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, service = workspace
    entered, release, second_started = Event(), Event(), Event()
    calls: list[str] = []
    monkeypatch.setattr("ridge.jobs.subprocess.Popen", Mock())

    def validate(_guard: FilesystemFootprints, _path: str, _roots: tuple[str, ...]) -> bool:
        calls.append(_path)
        entered.set()
        assert release.wait(5)
        return True

    def second() -> str:
        second_started.set()
        return service.submit_write("a", "dir/a", b"once", idempotency_key="concurrent").id

    monkeypatch.setattr(FilesystemFootprints, "validate_footprint", validate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            service.submit_write, "a", "dir/a", b"once", idempotency_key="concurrent"
        )
        try:
            assert entered.wait(5)
            repeated = pool.submit(second)
            assert second_started.wait(5)
        finally:
            release.set()
        assert first.result(timeout=5).id == repeated.result(timeout=5)
    assert calls == ["dir/a"]


def test_background_unavailable_probe_keeps_durable_failure_workflow(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, service = workspace
    monkeypatch.setattr("ridge.jobs.subprocess.Popen", Mock())
    monkeypatch.setattr(
        FilesystemFootprints, "validate_footprint", Mock(side_effect=OSError("unavailable probe"))
    )
    job = service.submit_write("a", "dir/a", b"attempt later")
    assert service.inspect_lock(job.id)["claims"] == [
        {"domain": "files", "scope": None, "mode": "exclusive"}
    ]
    assert (root / "dir/a").read_bytes() == b"initial"


@pytest.mark.parametrize("provider", ["docker", "ssh"])
def test_different_transport_contexts_disable_narrowing(provider: str, tmp_path: Path) -> None:
    if provider == "docker":
        first = DockerResource(
            "a", container="fixture", root="/workspace", python_executable="python3"
        )
        second = DockerResource(
            "b", container="fixture", root="/workspace", python_executable="another-python"
        )
    else:
        first = SshResource(
            "a",
            host="fixture",
            root="/workspace",
            python_executable="python3",
            identity_file=tmp_path / "key-one",
        )
        second = SshResource(
            "b",
            host="fixture",
            root="/workspace",
            python_executable="python3",
            identity_file=tmp_path / "key-two",
        )
    planner = FootprintPlanner(ResourceRegistry((first, second)), {"a": "shared", "b": "shared"})
    assert planner.plan((JobScope("a", WRITE),), ("file",)) == (Claim(None, "exclusive", "shared"),)
