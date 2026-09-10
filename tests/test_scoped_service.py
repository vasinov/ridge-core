from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import cast
from unittest.mock import Mock

import pytest

from ridge._access import AccessGrant, IssuedScope, ScopeAccessError, ScopeStore
from ridge.application import RidgeService
from ridge.backends.local import LocalResource
from ridge.config import LoadedConfiguration, load_configuration
from ridge.errors import AuthorizationDeniedError, LockConflictError, ResourceNotFoundError
from ridge.model import JobScope, JobStatus, Operation

READ = Operation.DATA_READ
WRITE = Operation.DATA_WRITE
Workspace = tuple[Path, LoadedConfiguration, ScopeStore, IssuedScope, IssuedScope, IssuedScope]


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    (tmp_path / "data").mkdir()
    path = tmp_path / "ridge.yaml"
    path.write_text(
        "resources:\n  a: {provider: local, root: data, lock_key: shared}\n"
        "  b: {provider: local, root: data, lock_key: shared}\n"
        "  hidden: {provider: local, root: data}\n"
        "delegation: {a: [data.read, data.write], b: [data.read, data.write]}\n"
    )
    loaded = load_configuration(path)
    store = ScopeStore(tmp_path / ".ridge")
    parent = store.issue(
        loaded, [AccessGrant("a", frozenset({READ, WRITE}), frozenset({READ, WRITE}))]
    )
    left = store.issue(
        loaded, [AccessGrant("a", frozenset({READ, WRITE}))], actor_token=parent.token
    )
    right = store.issue(
        loaded, [AccessGrant("a", frozenset({READ, WRITE}))], actor_token=parent.token
    )
    return path, loaded, store, parent, left, right


def test_bound_requests_refresh_policy_identity_and_discovery(workspace: Workspace) -> None:
    path, loaded, store, parent, left, _right = workspace
    service = RidgeService.from_config(path, scope_token=left.token)
    assert [resource.name for resource in service.list_resources()] == ["a"]
    for name in ("b", "hidden", "unknown"):
        with pytest.raises(ResourceNotFoundError):
            service.inspect_resource(name)
    service.write_data("a", "value", b"original")
    original = path.read_text()
    path.write_text(original + "permissions: {a: [data.read]}\n")
    assert service.read_data("a", "value") == b"original"
    with pytest.raises(AuthorizationDeniedError):
        service.write_data("a", "value", b"denied")
    assert (path.parent / "data/value").read_bytes() == b"original"
    path.write_text(original + "# harmless\n")
    service.write_data("a", "value", b"restored")
    store.revoke(loaded, parent.scope.id)
    with pytest.raises(ScopeAccessError, match="revoked"):
        service.list_resources()


def test_bound_scope_preserves_alias_coordination_and_hides_siblings(workspace: Workspace) -> None:
    path, loaded, store, _parent, left, right = workspace
    one = RidgeService.from_config(path, scope_token=left.token)
    two = RidgeService.from_config(path, scope_token=right.token)
    operator = RidgeService.from_config(path)
    held = one.acquire_locks([JobScope("a", WRITE)])
    with pytest.raises(LockConflictError):
        operator.write_data("b", "denied", b"no")
    assert two.list_locks()["entries"] == []
    with pytest.raises(ScopeAccessError, match="unavailable"):
        two.inspect_lock(str(held["id"]))
    with pytest.raises(ScopeAccessError, match="unavailable"):
        two.inspect_lock("unknown-lock")
    for action in (two.release_locks, two.renew_locks):
        with pytest.raises(ScopeAccessError, match="unavailable"):
            action(str(held["token"]))
        with pytest.raises(ScopeAccessError, match="unavailable"):
            action("unknown-token")
    with pytest.raises(ScopeAccessError, match="unavailable"):
        two.with_lock(str(held["token"])).write_data("a", "denied", b"no")
    one.with_lock(str(held["token"])).write_data("a", "owned", b"yes")
    store.revoke(loaded, left.scope.id)
    assert operator.inspect_lock(str(held["id"]))["status"] == "open"
    operator.release_locks(str(held["token"]))
    assert not (path.parent / "data/denied").exists()


@pytest.mark.parametrize("background", [False, True])
def test_closure_between_preflight_and_admission_denies_before_effects(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    background: bool,
) -> None:
    path, loaded, store, _parent, left, _right = workspace
    service = RidgeService.from_config(path, scope_token=left.token)
    original = RidgeService._local_scopes  # pyright: ignore[reportPrivateUsage]

    def close(self: RidgeService, scopes: Sequence[JobScope]) -> bool:
        store.revoke(loaded, left.scope.id)
        return original(self, scopes)

    monkeypatch.setattr(RidgeService, "_local_scopes", close)
    with pytest.raises(ScopeAccessError, match="revoked"):
        if background:
            service.submit_write("a", "denied", b"no")
        else:
            service.write_data("a", "denied", b"no")
    assert not (path.parent / "data/denied").exists()
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM lock_operations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_revocation_does_not_hold_or_release_admitted_operation(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, loaded, store, _parent, left, _right = workspace
    service = RidgeService.from_config(path, scope_token=left.token)
    operator = RidgeService.from_config(path)
    entered, finish = Event(), Event()
    write = LocalResource.write

    def blocked(self: LocalResource, path: str, content: bytes) -> None:
        entered.set()
        assert finish.wait(5)
        return write(self, path, content)

    monkeypatch.setattr(LocalResource, "write", blocked)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(service.write_data, "a", "admitted", b"yes")
        try:
            assert entered.wait(5)
            store.revoke(loaded, left.scope.id)
            entries = operator.list_locks()["entries"]
            assert len(cast(list[object], entries)) == 1
            with pytest.raises(LockConflictError):
                operator.write_data("b", "admitted", b"no")
        finally:
            finish.set()
        result.result(timeout=5)
    assert (path.parent / "data/admitted").read_bytes() == b"yes"
    assert operator.list_locks()["entries"] == []


def test_jobs_are_owned_and_idempotency_is_scope_local(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, loaded, store, parent, left, right = workspace
    monkeypatch.setattr("ridge.jobs.subprocess.Popen", Mock())
    one = RidgeService.from_config(path, scope_token=left.token)
    two = RidgeService.from_config(path, scope_token=right.token)
    ancestor = RidgeService.from_config(path, scope_token=parent.token)
    operator = RidgeService.from_config(path)
    first = one.submit_write("a", "job", b"yes", idempotency_key="same")
    assert first.access_scope_id == left.scope.id
    assert one.inspect_job(first.id).access_scope_id == left.scope.id
    assert operator.inspect_job(first.id).access_scope_id == left.scope.id
    assert one.submit_write("a", "job", b"yes", idempotency_key="same").id == first.id
    assert two.list_jobs().jobs == ()
    for method in (two.inspect_job, two.cancel_job):
        with pytest.raises(ScopeAccessError, match="unavailable"):
            method(first.id)
    with pytest.raises(ScopeAccessError, match="unavailable"):
        two.read_job_log(first.id, "stdout")
    assert ancestor.inspect_job(first.id).access_scope_id == left.scope.id
    cancelled = ancestor.cancel_job(first.id)
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.access_scope_id == left.scope.id
    second = two.submit_write("a", "job", b"yes", idempotency_key="same")
    assert second.id != first.id
    store.revoke(loaded, right.scope.id)
    with pytest.raises(ScopeAccessError, match="revoked"):
        two.inspect_job(second.id)
    assert ancestor.inspect_job(second.id).access_scope_id == right.scope.id
    assert operator.cancel_job(second.id).status is JobStatus.CANCELLED
