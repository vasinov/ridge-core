"""Internal durable task authority; frontend admission is wired separately.

Callers supply one freshly loaded, checked configuration per request. A resolved
access value is an admission snapshot, not a reusable credential or resource lock.
Only trusted application code may select the operator context (token=None).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from ridge.config import (
    ConfigurationDocument,
    LoadedConfiguration,
    construct_configuration,
    read_configuration,
)
from ridge.errors import AuthorizationDeniedError
from ridge.model import Operation
from ridge.provider import ResourceProviderRegistry

_MAX_DEPTH = 32
ScopeStatus = Literal["active", "revoked", "expired", "invalidated"]


class ScopeAccessError(AuthorizationDeniedError):
    """A bounded reason without credentials or another task's metadata."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"scope access denied: {reason}")


class _ClosedScopeError(ScopeAccessError):
    """Commit an observed terminal lifecycle transition before denying access."""


@dataclass(frozen=True, slots=True)
class AccessGrant:
    resource: str
    operations: frozenset[Operation]
    delegation: frozenset[Operation] = frozenset()

    def __post_init__(self) -> None:
        if type(self.resource) is not str or not self.resource:
            raise ValueError("a grant requires a resource name")
        for name in ("operations", "delegation"):
            values = getattr(self, name)
            if not isinstance(values, frozenset) or any(
                not isinstance(operation, Operation)
                for operation in cast(frozenset[object], values)
            ):
                raise ValueError(f"{name} must be a frozenset of operations")


@dataclass(frozen=True, slots=True)
class ScopeInfo:
    id: str
    parent_id: str | None
    grants: tuple[AccessGrant, ...]
    created_at: datetime
    expires_at: datetime | None
    status: ScopeStatus


@dataclass(frozen=True, slots=True)
class IssuedScope:
    scope: ScopeInfo
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ScopeAccess:
    scope: ScopeInfo
    lineage: tuple[str, ...]
    grants: tuple[AccessGrant, ...]

    def allows(self, resource: str, operation: Operation, *, delegate: bool = False) -> bool:
        return any(
            grant.resource == resource
            and operation in (grant.delegation if delegate else grant.operations)
            for grant in self.grants
        )


@dataclass(frozen=True, slots=True)
class ScopePage:
    scopes: tuple[ScopeInfo, ...]
    next_cursor: str | None


def _token_hash(token: str) -> str:
    if type(token) is not str or not token or len(token) > 256:
        raise ScopeAccessError("invalid_token")
    return hashlib.sha256(token.encode()).hexdigest()


def _grants_json(grants: Sequence[AccessGrant]) -> str:
    return json.dumps(
        [
            {
                "resource": grant.resource,
                "operations": sorted(operation.value for operation in grant.operations),
                "delegation": sorted(operation.value for operation in grant.delegation),
            }
            for grant in sorted(grants, key=lambda grant: grant.resource)
        ],
        sort_keys=True,
    )


def _read_grants(value: str) -> tuple[AccessGrant, ...]:
    return tuple(
        AccessGrant(
            grant["resource"],
            frozenset(Operation(operation) for operation in grant["operations"]),
            frozenset(Operation(operation) for operation in grant["delegation"]),
        )
        for grant in json.loads(value)
    )


class ScopeStore:
    """Immutable issued grants, hashed handles, and serialized scope lifecycle.

    This internal store does not dispatch operations or authorize job/lock access.
    Its transaction can be used by admission code to serialize with revocation;
    never hold a SQLite transaction while executing provider operations.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self.database = self.directory / "state.sqlite3"

    def connect(self) -> sqlite3.Connection:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS access_scopes (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT UNIQUE NOT NULL,
                    parent_id TEXT REFERENCES access_scopes(id),
                    config_path TEXT NOT NULL,
                    state_directory TEXT NOT NULL,
                    grants_json TEXT NOT NULL,
                    identities_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    status TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS access_scopes_parent ON access_scopes(parent_id);
                CREATE INDEX IF NOT EXISTS access_scopes_workspace ON access_scopes(config_path, id);
            """)
            return connection
        except BaseException:
            connection.close()
            raise

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except _ClosedScopeError:
                # Observed expiry/identity invalidation is terminal even when the
                # triggering request is denied. Other errors roll back normally.
                connection.commit()
                raise
            else:
                connection.commit()
        finally:
            connection.close()

    def _workspace(self, loaded: LoadedConfiguration | ConfigurationDocument) -> str:
        if loaded.path is None or loaded.state_directory is None:
            raise ScopeAccessError("workspace_required")
        if loaded.state_directory.resolve() != self.directory:
            raise ScopeAccessError("workspace_changed")
        return str(loaded.path.resolve())

    @staticmethod
    def _row(connection: sqlite3.Connection, identity: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM access_scopes WHERE id = ?", (identity,)).fetchone()
        if row is None:
            raise ScopeAccessError("unavailable")
        return row

    def _lineage(
        self, connection: sqlite3.Connection, row: sqlite3.Row, config_path: str
    ) -> tuple[sqlite3.Row, ...]:
        rows: list[sqlite3.Row] = []
        seen: set[str] = set()
        while True:
            if (
                row["id"] in seen
                or len(rows) >= _MAX_DEPTH
                or row["config_path"] != config_path
                or row["state_directory"] != str(self.directory)
            ):
                raise ScopeAccessError("unavailable")
            seen.add(row["id"])
            rows.append(row)
            if row["parent_id"] is None:
                return tuple(reversed(rows))
            row = self._row(connection, row["parent_id"])

    @staticmethod
    def _status(
        connection: sqlite3.Connection,
        rows: Sequence[sqlite3.Row],
        loaded: LoadedConfiguration | ConfigurationDocument,
        now: datetime,
    ) -> ScopeStatus:
        for row in rows:
            status = row["status"]
            if status in {"revoked", "expired", "invalidated"}:
                return status
            if status != "active":
                raise ScopeAccessError("unavailable")
            if row["expires_at"] is not None and datetime.fromisoformat(row["expires_at"]) <= now:
                status = "expired"
            elif any(
                loaded.resource_identities.get(name) != identity
                for name, identity in json.loads(row["identities_json"]).items()
            ):
                status = "invalidated"
            else:
                continue
            connection.execute(
                "UPDATE access_scopes SET status = ? WHERE id = ?", (status, row["id"])
            )
            return status
        return "active"

    @staticmethod
    def _info(row: sqlite3.Row, status: ScopeStatus) -> ScopeInfo:
        return ScopeInfo(
            row["id"],
            row["parent_id"],
            _read_grants(row["grants_json"]),
            datetime.fromisoformat(row["created_at"]),
            datetime.fromisoformat(row["expires_at"]) if row["expires_at"] is not None else None,
            status,
        )

    def resolve(
        self,
        loaded: LoadedConfiguration,
        token: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> ScopeAccess:
        """Resolve a bearer handle against current policy and every ancestor."""
        self._workspace(loaded)
        _token_hash(token)
        if connection is None:
            with self.transaction() as opened:
                return self.resolve(loaded, token, connection=opened)
        rows = self._active_rows(connection, loaded, token)
        row = rows[-1]
        ceilings = [
            {grant.resource: grant.delegation for grant in _read_grants(parent["grants_json"])}
            for parent in rows[:-1]
        ]
        effective: list[AccessGrant] = []
        for grant in _read_grants(row["grants_json"]):
            ceiling = frozenset(
                operation
                for operation in Operation
                if loaded.authorization.allows(grant.resource, operation)
                and loaded.delegation.allows(grant.resource, operation)
                and all(operation in ancestor.get(grant.resource, ()) for ancestor in ceilings)
            )
            effective.append(
                AccessGrant(grant.resource, grant.operations & ceiling, grant.delegation & ceiling)
            )
        return ScopeAccess(
            self._info(row, "active"), tuple(parent["id"] for parent in rows), tuple(effective)
        )

    def _active_rows(
        self,
        connection: sqlite3.Connection,
        loaded: LoadedConfiguration | ConfigurationDocument,
        token: str,
    ) -> tuple[sqlite3.Row, ...]:
        config_path = self._workspace(loaded)
        row = connection.execute(
            "SELECT * FROM access_scopes WHERE token_hash = ?",
            (_token_hash(token),),
        ).fetchone()
        if row is None:
            raise ScopeAccessError("invalid_token")
        rows = self._lineage(connection, row, config_path)
        status = self._status(connection, rows, loaded, datetime.now(UTC))
        if status != "active":
            raise _ClosedScopeError(status)
        return rows

    def load_configuration(
        self,
        config_path: str | Path,
        token: str,
        *,
        providers: ResourceProviderRegistry | None = None,
    ) -> tuple[LoadedConfiguration, ScopeAccess]:
        """Check lifecycle before providers, then resolve against the same document.

        No transaction spans provider construction. Closure during construction
        is caught by the second check. The returned snapshot is not execution
        admission: the operation must check again in its claim transaction.
        """
        _token_hash(token)
        document = read_configuration(config_path)
        self._workspace(document)
        with self.transaction() as connection:
            self._active_rows(connection, document, token)
        loaded = construct_configuration(document, providers=providers)
        return loaded, self.resolve(loaded, token)

    def issue(
        self,
        loaded: LoadedConfiguration,
        grants: Sequence[AccessGrant],
        *,
        actor_token: str | None = None,
        expires_at: datetime | None = None,
    ) -> IssuedScope:
        config_path = self._workspace(loaded)
        grants = tuple(grants)
        if not 1 <= len(grants) <= 100 or len({grant.resource for grant in grants}) != len(grants):
            raise ValueError("declare 1 to 100 distinct resource grants")
        if any(not (grant.operations | grant.delegation) for grant in grants):
            raise ValueError("each resource requires use or delegation authority")
        if expires_at is not None:
            if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                raise ValueError("expires_at requires an absolute timezone-aware datetime")
            expires_at = expires_at.astimezone(UTC)
        with self.transaction() as connection:
            now = datetime.now(UTC)
            parent = (
                self.resolve(loaded, actor_token, connection=connection)
                if actor_token is not None
                else None
            )
            if parent is not None:
                if len(parent.lineage) >= _MAX_DEPTH:
                    raise ValueError("scope nesting exceeds 32 levels")
                if parent.scope.expires_at is not None:
                    if expires_at is None:
                        expires_at = parent.scope.expires_at
                    elif expires_at > parent.scope.expires_at:
                        raise ScopeAccessError("expiry_exceeds_parent")
            if expires_at is not None and expires_at <= now:
                raise ValueError("expires_at must be in the future")
            for grant in grants:
                if grant.resource not in loaded.resource_identities:
                    raise ScopeAccessError("not_delegable")
                for operation in grant.operations | grant.delegation:
                    if (
                        not loaded.authorization.allows(grant.resource, operation)
                        or not loaded.delegation.allows(grant.resource, operation)
                        or (
                            parent is not None
                            and not parent.allows(grant.resource, operation, delegate=True)
                        )
                    ):
                        raise ScopeAccessError("not_delegable")
            token = secrets.token_urlsafe(32)
            identity = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO access_scopes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
                (
                    identity,
                    hashlib.sha256(token.encode()).hexdigest(),
                    parent.scope.id if parent is not None else None,
                    config_path,
                    str(self.directory),
                    _grants_json(grants),
                    json.dumps(
                        {
                            grant.resource: loaded.resource_identities[grant.resource]
                            for grant in grants
                        }
                    ),
                    now.isoformat(),
                    expires_at.isoformat() if expires_at is not None else None,
                ),
            )
            return IssuedScope(self._info(self._row(connection, identity), "active"), token)

    def _visible(
        self,
        connection: sqlite3.Connection,
        loaded: LoadedConfiguration,
        identity: str,
        actor_token: str | None,
    ) -> tuple[sqlite3.Row, ...]:
        actor = (
            self.resolve(loaded, actor_token, connection=connection)
            if actor_token is not None
            else None
        )
        rows = self._lineage(connection, self._row(connection, identity), self._workspace(loaded))
        if actor is not None and actor.scope.id not in {row["id"] for row in rows}:
            raise ScopeAccessError("unavailable")
        return rows

    def inspect(
        self, loaded: LoadedConfiguration, identity: str, *, actor_token: str | None = None
    ) -> ScopeInfo:
        self._workspace(loaded)
        with self.transaction() as connection:
            rows = self._visible(connection, loaded, identity, actor_token)
            return self._info(rows[-1], self._status(connection, rows, loaded, datetime.now(UTC)))

    def revoke(
        self, loaded: LoadedConfiguration, identity: str, *, actor_token: str | None = None
    ) -> ScopeInfo:
        self._workspace(loaded)
        with self.transaction() as connection:
            rows = self._visible(connection, loaded, identity, actor_token)
            status = self._status(connection, rows, loaded, datetime.now(UTC))
            if status == "active":
                connection.execute(
                    "UPDATE access_scopes SET status = 'revoked' WHERE id = ?", (identity,)
                )
                status = "revoked"
            return self._info(rows[-1], status)

    def list(
        self,
        loaded: LoadedConfiguration,
        *,
        actor_token: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> ScopePage:
        config_path = self._workspace(loaded)
        if isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if cursor is not None:
            try:
                uuid.UUID(cursor)
            except ValueError as exc:
                raise ValueError("invalid scope cursor") from exc
        with self.transaction() as connection:
            actor = (
                self.resolve(loaded, actor_token, connection=connection)
                if actor_token is not None
                else None
            )
            rows = connection.execute(
                """
                WITH RECURSIVE visible(id) AS (
                    SELECT id FROM access_scopes WHERE id = ?
                    UNION ALL
                    SELECT child.id FROM access_scopes child JOIN visible ON child.parent_id = visible.id
                )
                SELECT * FROM access_scopes
                WHERE config_path = ? AND state_directory = ? AND id > ?
                    AND (? IS NULL OR id IN (SELECT id FROM visible))
                ORDER BY id LIMIT ?
                """,
                (
                    actor.scope.id if actor else None,
                    config_path,
                    str(self.directory),
                    cursor or "",
                    actor.scope.id if actor else None,
                    limit + 1,
                ),
            ).fetchall()
            now = datetime.now(UTC)
            infos = tuple(
                self._info(
                    row,
                    self._status(
                        connection, self._lineage(connection, row, config_path), loaded, now
                    ),
                )
                for row in rows[:limit]
            )
            return ScopePage(infos, infos[-1].id if len(rows) > limit else None)
