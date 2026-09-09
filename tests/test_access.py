from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

from ridge._access import AccessGrant, ScopeAccessError, ScopeStore
from ridge.authorization import AuthorizationPolicy
from ridge.config import LoadedConfiguration, load_configuration
from ridge.coordination import Coordination
from ridge.model import JobScope, Operation

READ = Operation.DATA_READ
WRITE = Operation.DATA_WRITE
DELETE = Operation.DATA_DELETE


@pytest.fixture
def loaded(tmp_path: Path) -> LoadedConfiguration:
    (tmp_path / "data").mkdir()
    config = tmp_path / "ridge.yaml"
    config.write_text(
        "resources: {data: {provider: local, root: data, lock_key: shared}}\n"
        "permissions: {data: [data.read, data.write]}\n"
        "delegation: {data: [data.read, data.write]}\n"
    )
    return load_configuration(config)


@pytest.fixture
def store(loaded: LoadedConfiguration) -> ScopeStore:
    assert loaded.state_directory is not None
    return ScopeStore(loaded.state_directory)


def grant(*operations: Operation, delegate: tuple[Operation, ...] = ()) -> AccessGrant:
    return AccessGrant("data", frozenset(operations), frozenset(delegate))


def test_issue_reconnect_hashes_and_immutable_grants(
    loaded: LoadedConfiguration, store: ScopeStore
) -> None:
    issued = store.issue(loaded, [grant(READ)])
    assert issued.token not in repr(issued)
    access = ScopeStore(store.directory).resolve(loaded, issued.token)
    assert access.lineage == (issued.scope.id,)
    assert access.allows("data", READ)
    assert not access.allows("data", WRITE)
    assert not access.allows("data", READ, delegate=True)
    assert not access.allows("other", READ)
    assert access.scope == issued.scope
    assert access.grants == issued.scope.grants
    with store.connect() as connection:
        row = connection.execute("SELECT * FROM access_scopes").fetchone()
        assert issued.token not in repr(tuple(row))
        assert len(row["token_hash"]) == 64
    assert issued.token.encode() not in store.database.read_bytes()


@pytest.mark.parametrize("token", ["", "wrong", "x" * 257])
def test_invalid_tokens_never_select_operator(
    loaded: LoadedConfiguration, store: ScopeStore, token: str
) -> None:
    with pytest.raises(ScopeAccessError, match="invalid_token"):
        store.resolve(loaded, token)
    with pytest.raises(ScopeAccessError, match="invalid_token"):
        store.issue(loaded, [grant(READ)], actor_token=token)
    with pytest.raises(ScopeAccessError, match="invalid_token"):
        store.list(loaded, actor_token=token)


def test_operator_and_parent_ceilings(loaded: LoadedConfiguration, store: ScopeStore) -> None:
    parent = store.issue(loaded, [grant(READ, delegate=(READ,))])
    child = store.issue(loaded, [grant(READ)], actor_token=parent.token)
    assert store.resolve(loaded, child.token).lineage == (parent.scope.id, child.scope.id)
    for requested, actor in (
        (grant(WRITE), parent.token),
        (grant(DELETE), None),
        (grant(READ, delegate=(WRITE,)), parent.token),
        (grant(READ), child.token),
    ):
        with pytest.raises(ScopeAccessError, match="not_delegable"):
            store.issue(loaded, [requested], actor_token=actor)
    assert len(store.list(loaded).scopes) == 2


def test_use_and_delegation_are_distinct(loaded: LoadedConfiguration, store: ScopeStore) -> None:
    parent = store.issue(loaded, [grant(delegate=(READ,))])
    assert not store.resolve(loaded, parent.token).allows("data", READ)
    child = store.issue(loaded, [grant(READ)], actor_token=parent.token)
    assert store.resolve(loaded, child.token).allows("data", READ)


def test_policy_cannot_widen_issued_grants(loaded: LoadedConfiguration, store: ScopeStore) -> None:
    parent = store.issue(loaded, [grant(READ, WRITE, delegate=(READ,))])
    child = store.issue(loaded, [grant(READ)], actor_token=parent.token)
    wider = replace(
        loaded,
        authorization=AuthorizationPolicy.unrestricted(),
        delegation=AuthorizationPolicy.unrestricted(),
    )
    assert not store.resolve(wider, child.token).allows("data", WRITE)
    assert not store.resolve(wider, child.token).allows("data", READ, delegate=True)
    assert not store.resolve(wider, parent.token).allows("data", WRITE, delegate=True)
    for field in ("authorization", "delegation"):
        reduced = replace(loaded, **{field: AuthorizationPolicy.exact({})})
        assert not store.resolve(reduced, child.token).allows("data", READ)
        with pytest.raises(ScopeAccessError, match="not_delegable"):
            store.issue(reduced, [grant(READ)], actor_token=parent.token)
    assert store.resolve(loaded, child.token).allows("data", READ)


def test_grants_are_not_locks(loaded: LoadedConfiguration, store: ScopeStore) -> None:
    coordination = Coordination(store.directory, loaded.lock_keys)
    held = coordination.acquire([JobScope("data", WRITE)])
    issued = store.issue(loaded, [grant(WRITE)])
    store.revoke(loaded, issued.scope.id)
    assert len(coordination.page()) == 1
    assert coordination.inspect(str(held["id"]))["status"] == "open"
    coordination.session_action(str(held["token"]), release=True)
    assert coordination.page() == []


def test_subtree_visibility_and_closure(loaded: LoadedConfiguration, store: ScopeStore) -> None:
    parent = store.issue(loaded, [grant(READ, delegate=(READ,))])
    child = store.issue(loaded, [grant(READ, delegate=(READ,))], actor_token=parent.token)
    grandchild = store.issue(loaded, [grant(READ)], actor_token=child.token)
    sibling = store.issue(loaded, [grant(READ)], actor_token=parent.token)
    assert {scope.id for scope in store.list(loaded, actor_token=child.token).scopes} == {
        child.scope.id,
        grandchild.scope.id,
    }
    for target in (parent.scope.id, sibling.scope.id, "unknown"):
        for action in (store.inspect, store.revoke):
            with pytest.raises(ScopeAccessError, match="unavailable"):
                action(loaded, target, actor_token=child.token)
    assert store.revoke(loaded, child.scope.id, actor_token=parent.token).status == "revoked"
    for token in (child.token, grandchild.token):
        for call in (
            lambda token=token: store.resolve(loaded, token),
            lambda token=token: store.list(loaded, actor_token=token),
            lambda token=token: store.inspect(loaded, grandchild.scope.id, actor_token=token),
        ):
            with pytest.raises(ScopeAccessError, match="revoked"):
                call()
    assert store.inspect(loaded, grandchild.scope.id, actor_token=parent.token).status == "revoked"
    assert store.resolve(loaded, sibling.token).allows("data", READ)
    assert store.revoke(loaded, child.scope.id).status == "revoked"


def test_scope_can_close_itself(loaded: LoadedConfiguration, store: ScopeStore) -> None:
    issued = store.issue(loaded, [grant(READ)])
    assert store.revoke(loaded, issued.scope.id, actor_token=issued.token).status == "revoked"
    with pytest.raises(ScopeAccessError, match="revoked"):
        store.resolve(loaded, issued.token)


def test_expiry_bounds_and_parent_closure(loaded: LoadedConfiguration, store: ScopeStore) -> None:
    expiry = datetime.now(UTC) + timedelta(hours=1)
    parent = store.issue(loaded, [grant(READ, delegate=(READ,))], expires_at=expiry)
    child = store.issue(loaded, [grant(READ)], actor_token=parent.token)
    assert child.scope.expires_at == expiry
    with pytest.raises(ScopeAccessError, match="expiry_exceeds_parent"):
        store.issue(
            loaded,
            [grant(READ)],
            actor_token=parent.token,
            expires_at=expiry + timedelta(seconds=1),
        )
    for invalid in (
        datetime.now(UTC).replace(tzinfo=None),
        datetime.now(UTC) - timedelta(seconds=1),
    ):
        with pytest.raises(ValueError, match="expires_at"):
            store.issue(loaded, [grant(READ)], expires_at=invalid)
    with store.connect() as connection:
        connection.execute(
            "UPDATE access_scopes SET expires_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), parent.scope.id),
        )
    with pytest.raises(ScopeAccessError, match="expired"):
        store.resolve(loaded, child.token)
    assert store.inspect(loaded, child.scope.id).status == "expired"
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT status FROM access_scopes WHERE id = ?", (parent.scope.id,)
            ).fetchone()[0]
            == "expired"
        )


def test_ancestor_identity_change_closes_even_unreferenced_descendant(
    loaded: LoadedConfiguration, store: ScopeStore
) -> None:
    expanded = replace(
        loaded,
        resource_identities={**loaded.resource_identities, "other": "v1"},
        authorization=AuthorizationPolicy.unrestricted(),
        delegation=AuthorizationPolicy.unrestricted(),
    )
    parent = store.issue(
        expanded, [grant(READ, delegate=(READ,)), AccessGrant("other", frozenset({READ}))]
    )
    child = store.issue(expanded, [grant(READ)], actor_token=parent.token)
    changed = replace(expanded, resource_identities={**expanded.resource_identities, "other": "v2"})
    with pytest.raises(ScopeAccessError, match="invalidated"):
        store.resolve(changed, child.token)
    with pytest.raises(ScopeAccessError, match="invalidated"):
        store.resolve(expanded, child.token)
    assert store.inspect(changed, child.scope.id).status == "invalidated"


def test_harmless_edits_and_unrelated_resources(
    loaded: LoadedConfiguration, store: ScopeStore
) -> None:
    assert loaded.path is not None
    issued = store.issue(loaded, [grant(READ)])
    loaded.path.write_text(
        "# harmless presentation and unrelated resource\n"
        "delegation: {data: [data.write, data.read]}\n"
        "permissions: {data: [data.write, data.read]}\n"
        "resources: {data: {lock_key: shared, root: data, provider: local}, other: {provider: local}}\n"
    )
    assert store.resolve(load_configuration(loaded.path), issued.token).allows("data", READ)


@pytest.mark.parametrize("change", ["root", "lock_key", "provider", "removed"])
def test_real_config_identity_changes_close_access(
    loaded: LoadedConfiguration, store: ScopeStore, change: str
) -> None:
    assert loaded.path is not None
    issued = store.issue(loaded, [grant(READ)])
    original = loaded.path.read_text()
    if change == "removed":
        edited = "resources: {}"
    else:
        edited = original.replace(
            {
                "root": "root: data",
                "lock_key": "lock_key: shared",
                "provider": "provider: local, root: data",
            }[change],
            {
                "root": "root: .",
                "lock_key": "lock_key: different",
                "provider": "provider: s3, bucket: example",
            }[change],
        )
    loaded.path.write_text(edited)
    with pytest.raises(ScopeAccessError, match="invalidated"):
        store.resolve(load_configuration(loaded.path), issued.token)


def test_workspace_identity_and_cross_inventory_visibility(
    loaded: LoadedConfiguration, store: ScopeStore
) -> None:
    assert loaded.path is not None
    issued = store.issue(loaded, [grant(READ)])
    other = replace(loaded, path=loaded.path.with_name("other.yaml"))
    assert store.list(other).scopes == ()
    with pytest.raises(ScopeAccessError, match="unavailable"):
        store.resolve(other, issued.token)
    with pytest.raises(ScopeAccessError, match="unavailable"):
        store.inspect(other, issued.scope.id)
    moved = replace(loaded, state_directory=store.directory / "new")
    with pytest.raises(ScopeAccessError, match="workspace_changed"):
        store.resolve(moved, issued.token)
    assert not (store.directory / "new").exists()
    with pytest.raises(ScopeAccessError, match="workspace_required"):
        store.issue(replace(loaded, path=None), [grant(READ)])


def test_bounded_pagination(loaded: LoadedConfiguration, store: ScopeStore) -> None:
    ids = {store.issue(loaded, [grant(READ)]).scope.id for _ in range(5)}
    found: list[str] = []
    cursor = None
    while True:
        page = store.list(loaded, cursor=cursor, limit=2)
        assert len(page.scopes) <= 2
        found.extend(scope.id for scope in page.scopes)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert found == sorted(ids)
    for invalid in (0, 201, True):
        with pytest.raises(ValueError, match="limit"):
            store.list(loaded, limit=invalid)
    with pytest.raises(ValueError, match="cursor"):
        store.list(loaded, cursor="invalid")


def test_depth_and_request_bounds(loaded: LoadedConfiguration, store: ScopeStore) -> None:
    token = None
    for _ in range(32):
        token = store.issue(loaded, [grant(READ, delegate=(READ,))], actor_token=token).token
    with pytest.raises(ValueError, match="nesting"):
        store.issue(loaded, [grant(READ)], actor_token=token)
    for invalid in ([], [grant(READ), grant(WRITE)], [grant()]):
        with pytest.raises(ValueError):
            store.issue(loaded, invalid)
    with pytest.raises(ScopeAccessError, match="not_delegable"):
        store.issue(loaded, [AccessGrant("missing", frozenset({READ}))])


def test_issuance_and_revocation_serialize(loaded: LoadedConfiguration, store: ScopeStore) -> None:
    parent = store.issue(loaded, [grant(READ, delegate=(READ,))])
    ready = Event()

    def issue_after_revocation() -> None:
        ready.set()
        ScopeStore(store.directory).issue(loaded, [grant(READ)], actor_token=parent.token)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.transaction() as connection:
            connection.execute(
                "UPDATE access_scopes SET status = 'revoked' WHERE id = ?", (parent.scope.id,)
            )
            pending = executor.submit(issue_after_revocation)
            assert ready.wait(2)
        with pytest.raises(ScopeAccessError, match="revoked"):
            pending.result(timeout=5)
    assert len(store.list(loaded).scopes) == 1


def test_failure_rolls_back_transaction(loaded: LoadedConfiguration, store: ScopeStore) -> None:
    issued = store.issue(loaded, [grant(READ)])
    with pytest.raises(RuntimeError), store.transaction() as connection:
        connection.execute(
            "UPDATE access_scopes SET status = 'revoked' WHERE id = ?", (issued.scope.id,)
        )
        raise RuntimeError("interrupted")
    assert store.resolve(loaded, issued.token).allows("data", READ)
