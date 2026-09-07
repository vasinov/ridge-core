"""Persistent cooperative claims shared by CLI, MCP, and job supervisors."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import secrets
import sqlite3
import time
import uuid
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from ridge.errors import LockConflictError, LockOwnershipError
from ridge.model import JobScope, Operation


def scopes_value(scopes: Sequence[JobScope]) -> list[dict[str, str]]:
    return [{"resource": s.resource, "operation": s.operation.value} for s in scopes]


def read_scopes(value: str) -> tuple[JobScope, ...]:
    return tuple(JobScope(s["resource"], Operation(s["operation"])) for s in json.loads(value))


class Coordination:
    def __init__(self, directory: Path, keys: Mapping[str, str] | None = None) -> None:
        self.directory = directory
        self.database = directory / "state.sqlite3"
        self.keys = dict(keys or {})

    def connect(self) -> sqlite3.Connection:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS lock_sessions (
                id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
                scopes_json TEXT NOT NULL, claims_json TEXT NOT NULL,
                status TEXT NOT NULL, expires_at REAL NOT NULL, lease_seconds REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lock_operations (
                id TEXT PRIMARY KEY, session_id TEXT, job_id TEXT,
                scopes_json TEXT NOT NULL, claims_json TEXT NOT NULL,
                status TEXT NOT NULL, local_only INTEGER NOT NULL,
                reason TEXT
            );
            CREATE INDEX IF NOT EXISTS lock_operations_session ON lock_operations(session_id);
        """)
        return connection

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self.reconcile(connection)
            yield connection
            connection.commit()
        finally:
            connection.close()

    def claims(self, scopes: Sequence[JobScope]) -> dict[str, str]:
        claims: dict[str, str] = {}
        for scope in scopes:
            key = self.keys.get(scope.resource, scope.resource)
            mode = "shared" if scope.operation.effect == "read" else "exclusive"
            if claims.get(key) != "exclusive":
                claims[key] = mode
        return claims

    def reconcile(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE lock_sessions SET status = 'closing' WHERE status = 'open' AND expires_at <= ?",
            (time.time(),),
        )
        for row in connection.execute(
            "SELECT id FROM lock_operations WHERE status = 'active' AND job_id IS NULL"
        ).fetchall():
            path = self.directory / "operations" / (row["id"] + ".lock")
            with path.open("a+b") as owner:
                try:
                    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                connection.execute(
                    "UPDATE lock_operations SET status = 'uncertain', reason = ? WHERE id = ?",
                    ("caller disappeared; execution termination is unverified", row["id"]),
                )
        self.drain(connection)

    @staticmethod
    def drain(connection: sqlite3.Connection) -> None:
        connection.execute("""
            UPDATE lock_sessions SET status = 'released' WHERE status = 'closing'
            AND NOT EXISTS (SELECT 1 FROM lock_operations o
                WHERE o.session_id = lock_sessions.id AND o.status != 'released')
        """)

    @staticmethod
    def _conflicts(left: Mapping[str, str], right: Mapping[str, str]) -> bool:
        return any(key in right and "exclusive" in (mode, right[key]) for key, mode in left.items())

    def _check(
        self, connection: sqlite3.Connection, claims: dict[str, str], session: str | None
    ) -> None:
        for table in ("lock_sessions", "lock_operations"):
            rows = connection.execute(f"SELECT * FROM {table} WHERE status != 'released'")
            for row in rows:
                if table == "lock_sessions" and row["id"] == session:
                    continue
                if self._conflicts(claims, json.loads(row["claims_json"])):
                    # Do not disclose another inventory's resource names or operation IDs.
                    raise LockConflictError("resource claims conflict with existing ownership")

    @staticmethod
    def _session(connection: sqlite3.Connection, token: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM lock_sessions WHERE token_hash = ?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        ).fetchone()
        if row is None:
            raise LockOwnershipError("unknown lock token")
        return row

    def acquire(
        self, scopes: Sequence[JobScope], *, lease_seconds: float = 300, wait_seconds: float = 0
    ) -> dict[str, object]:
        if (
            isinstance(lease_seconds, bool)
            or not math.isfinite(lease_seconds)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError("lease_seconds must be between 1 and 3600")
        if (
            isinstance(wait_seconds, bool)
            or not math.isfinite(wait_seconds)
            or not 0 <= wait_seconds <= 60
        ):
            raise ValueError("wait_seconds must be between 0 and 60")
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                with self.transaction() as connection:
                    claims = self.claims(scopes)
                    self._check(connection, claims, None)
                    token = secrets.token_urlsafe(32)
                    identity = str(uuid.uuid4())
                    connection.execute(
                        "INSERT INTO lock_sessions VALUES (?, ?, ?, ?, 'open', ?, ?)",
                        (
                            identity,
                            hashlib.sha256(token.encode()).hexdigest(),
                            json.dumps(scopes_value(scopes)),
                            json.dumps(claims),
                            time.time() + lease_seconds,
                            lease_seconds,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM lock_sessions WHERE id = ?", (identity,)
                    ).fetchone()
                    assert row is not None
                    return {**self.view(row, "session"), "token": token}
            except LockConflictError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def session_action(self, token: str, *, release: bool = False) -> dict[str, object]:
        with self.transaction() as connection:
            row = self._session(connection, token)
            if release:
                connection.execute(
                    "UPDATE lock_sessions SET status = 'closing' WHERE id = ? AND status = 'open'",
                    (row["id"],),
                )
            else:
                if row["status"] != "open":
                    raise LockOwnershipError("session is closed or expired; acquire a new session")
                connection.execute(
                    "UPDATE lock_sessions SET expires_at = ? WHERE id = ?",
                    (time.time() + row["lease_seconds"], row["id"]),
                )
            self.drain(connection)
            updated = connection.execute(
                "SELECT * FROM lock_sessions WHERE id = ?", (row["id"],)
            ).fetchone()
            assert updated is not None
            return self.view(updated, "session")

    def admit(
        self,
        connection: sqlite3.Connection,
        identity: str,
        scopes: Sequence[JobScope],
        *,
        token: str | None,
        local_only: bool,
        job_id: str | None = None,
    ) -> None:
        self.reconcile(connection)
        claims = self.claims(scopes)
        session: str | None = None
        if token is not None:
            row = self._session(connection, token)
            if row["status"] != "open":
                raise LockOwnershipError("session is closed or expired; acquire a new session")
            declared = read_scopes(row["scopes_json"])
            if any(scope not in declared for scope in scopes):
                raise LockOwnershipError("operation was not declared by this session")
            held = json.loads(row["claims_json"])
            if any(
                key not in held or (mode == "exclusive" and held[key] != mode)
                for key, mode in claims.items()
            ):
                raise LockOwnershipError("resource lock keys changed; acquire a new session")
            session = row["id"]
        self._check(connection, claims, session)
        connection.execute(
            "INSERT INTO lock_operations VALUES (?, ?, ?, ?, ?, 'active', ?, NULL)",
            (
                identity,
                session,
                job_id,
                json.dumps(scopes_value(scopes)),
                json.dumps(claims),
                int(local_only),
            ),
        )
        if session is not None:
            connection.execute(
                "UPDATE lock_sessions SET expires_at = ? + lease_seconds WHERE id = ?",
                (time.time(), session),
            )

    @staticmethod
    def finish(
        connection: sqlite3.Connection, identity: str, *, safe: bool, reason: str | None = None
    ) -> None:
        connection.execute(
            "UPDATE lock_operations SET status = ?, reason = ? WHERE id = ? AND status != 'released'",
            ("released" if safe else "uncertain", reason, identity),
        )
        Coordination.drain(connection)

    @contextmanager
    def operation(
        self, scopes: Sequence[JobScope], *, token: str | None, local_only: bool
    ) -> Generator[None]:
        identity = str(uuid.uuid4())
        owners = self.directory / "operations"
        owners.mkdir(parents=True, exist_ok=True, mode=0o700)
        owner: BinaryIO
        owner_path = owners / (identity + ".lock")
        with owner_path.open("a+b") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            try:
                with self.transaction() as connection:
                    self.admit(connection, identity, scopes, token=token, local_only=local_only)
            except (LockConflictError, LockOwnershipError):
                # Rejected identities were never published to observers.
                owner_path.unlink(missing_ok=True)
                raise
            try:
                yield
            except BaseException as exc:
                # Exceptions do not prove that subprocesses or remote work stopped.
                safe = (
                    isinstance(exc, Exception)
                    and local_only
                    and len(scopes) == 1
                    and scopes[0].operation is not Operation.COMPUTE_EXEC
                )
                with self.transaction() as connection:
                    self.finish(
                        connection,
                        identity,
                        safe=safe,
                        reason=None
                        if safe
                        else "operation interrupted or failed; inspect effects before recovery",
                    )
                raise
            else:
                with self.transaction() as connection:
                    self.finish(connection, identity, safe=True)

    @staticmethod
    def view(row: sqlite3.Row, kind: str) -> dict[str, object]:
        result: dict[str, object] = {
            "id": row["id"],
            "kind": kind,
            "status": row["status"],
            "scopes": json.loads(row["scopes_json"]),
            "claims": json.loads(row["claims_json"]),
        }
        if kind == "session":
            result.update(expires_at=row["expires_at"], lease_seconds=row["lease_seconds"])
        else:
            result.update(session_id=row["session_id"], job_id=row["job_id"], reason=row["reason"])
        return result

    def inspect(self, identity: str) -> dict[str, object]:
        with self.transaction() as connection:
            for table, kind in (("lock_sessions", "session"), ("lock_operations", "operation")):
                row = connection.execute(
                    f"SELECT * FROM {table} WHERE id = ?", (identity,)
                ).fetchone()
                if row is not None:
                    return self.view(row, kind)
        raise LockOwnershipError("unknown lock identity")

    def token_scopes(self, token: str) -> tuple[JobScope, ...]:
        with self.transaction() as connection:
            return read_scopes(self._session(connection, token)["scopes_json"])

    def validate_job(self, identity: str, scopes: Sequence[JobScope]) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM lock_operations WHERE id = ? AND job_id = ?", (identity, identity)
            ).fetchone()
            if (
                row is None
                or row["status"] != "active"
                or any(s not in read_scopes(row["scopes_json"]) for s in scopes)
            ):
                raise LockOwnershipError("job no longer owns its resource claims")

    def force_release(self, identity: str, reason: str) -> None:
        if not reason.strip() or len(reason) > 1000:
            raise ValueError("a recovery reason of 1 to 1000 characters is required")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM lock_operations WHERE id = ?", (identity,)
            ).fetchone()
            if row is None or row["status"] != "uncertain":
                raise LockOwnershipError("force-release requires an uncertain operation identity")
            self.finish(connection, identity, safe=True, reason="force-released: " + reason)

    def page(self, *, after: str = "", limit: int = 100) -> list[dict[str, object]]:
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id, 'session' AS kind FROM lock_sessions WHERE status != 'released' AND id > ?
                UNION ALL
                SELECT id, 'operation' AS kind FROM lock_operations WHERE status != 'released' AND id > ?
                ORDER BY id LIMIT ?
            """,
                (after, after, limit),
            ).fetchall()
            result: list[dict[str, object]] = []
            for row in rows:
                table = "lock_sessions" if row["kind"] == "session" else "lock_operations"
                detail = connection.execute(
                    f"SELECT * FROM {table} WHERE id = ?", (row["id"],)
                ).fetchone()
                assert detail is not None
                result.append(self.view(detail, row["kind"]))
            return result
