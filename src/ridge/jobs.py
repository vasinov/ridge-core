"""Durable, immediate background jobs managed by a local supervisor."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from ridge._job_process import (
    GRACE_SECONDS,
    KILL_SECONDS,
    POLL_SECONDS,
    STARTUP_SECONDS,
    encode_json,
    timestamp,
)
from ridge.coordination import Coordination
from ridge.errors import JobConflictError, JobNotFoundError, RidgeError
from ridge.model import Job, JobKind, JobLog, JobPage, JobScope, JobStatus, JobSummary, Operation

_TERMINAL = {
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.LOST,
}
_LOG_LIMIT = 1024 * 1024


class JobManager:
    """SQLite job metadata plus per-job payload and log files."""

    def __init__(
        self,
        directory: Path,
        config_path: Path,
        config_fingerprint: str,
        *,
        lock_keys: Mapping[str, str] | None = None,
    ) -> None:
        self.directory = directory
        self.config_path = config_path
        self.config_fingerprint = config_fingerprint
        self.database = directory / "state.sqlite3"
        self.coordination = Coordination(directory, lock_keys)

    def submit(
        self,
        kind: JobKind,
        scopes: Sequence[JobScope],
        request: Mapping[str, object],
        *,
        payload: bytes | None = None,
        idempotency_key: str | None = None,
        lock_token: str | None = None,
        local_only: bool = False,
    ) -> Job:
        if idempotency_key == "":
            raise ValueError("idempotency_key must be non-empty or None")
        payload_digest = hashlib.sha256(payload).hexdigest() if payload is not None else None
        request_json = encode_json(dict(request))
        scopes_json = encode_json(
            [{"resource": scope.resource, "operation": scope.operation.value} for scope in scopes]
        )
        request_digest = hashlib.sha256(
            encode_json(
                {
                    "kind": kind.value,
                    "scopes": json.loads(scopes_json),
                    "request": json.loads(request_json),
                    "payload_sha256": payload_digest,
                    "config_fingerprint": self.config_fingerprint,
                    "config_path": str(self.config_path),
                    "lock_token_hash": hashlib.sha256(lock_token.encode()).hexdigest()
                    if lock_token
                    else None,
                }
            ).encode()
        ).hexdigest()
        connection = self.connect()
        try:
            # Serialize the idempotency lookup and insert across concurrent callers.
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key is not None:
                existing = connection.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if existing is not None:
                    if existing["request_digest"] != request_digest:
                        raise JobConflictError(
                            "idempotency key already belongs to a different job request"
                        )
                    existing_id = str(existing["id"])
                    connection.rollback()
                    connection.close()
                    return self.get(existing_id)

            job_id = str(uuid.uuid4())
            self.coordination.admit(
                connection, job_id, scopes, token=lock_token, local_only=local_only, job_id=job_id
            )
            job_directory = self.directory / job_id
            job_directory.mkdir(mode=0o700)
            payload_path: str | None = None
            try:
                if payload is not None:
                    payload_file = job_directory / "payload.bin"
                    payload_path = str(payload_file)
                    with payload_file.open("xb") as handle:
                        os.fchmod(handle.fileno(), 0o600)
                        handle.write(payload)
                submitted_at = timestamp()
                connection.execute(
                    """
                    INSERT INTO jobs (
                        id, kind, status, scopes_json, request_json, request_digest,
                        config_path, config_fingerprint, payload_path, idempotency_key,
                        submitted_at, cancellation_requested
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        job_id,
                        kind.value,
                        JobStatus.STARTING.value,
                        scopes_json,
                        request_json,
                        request_digest,
                        str(self.config_path),
                        self.config_fingerprint,
                        payload_path,
                        idempotency_key,
                        submitted_at,
                    ),
                )
                connection.commit()
            except BaseException:
                with suppress(OSError):
                    if payload_path is not None:
                        Path(payload_path).unlink(missing_ok=True)
                    job_directory.rmdir()
                raise
        finally:
            connection.close()

        try:
            subprocess.Popen(
                (sys.executable, "-m", "ridge._job_runner", "run", str(self.directory), job_id),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            self.finish(
                job_id,
                JobStatus.FAILED,
                local_termination_verified=True,
                error=f"cannot start job supervisor: {exc}",
            )
            raise RidgeError(f"cannot start job supervisor: {exc}") from exc
        return self.get(job_id)

    def list(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        allowed: Callable[[JobSummary], bool],
    ) -> JobPage:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("job limit must be between 1 and 200")
        store = hashlib.sha256(str(self.database.resolve()).encode()).hexdigest()
        after: tuple[str, str] | None = None
        if cursor is not None:
            try:
                if len(cursor) > 1024:
                    raise ValueError
                decoded: object = json.loads(base64.b64decode(cursor, validate=True))
                if not isinstance(decoded, list):
                    raise TypeError
                values = cast(list[object], decoded)
                if (
                    len(values) != 3
                    or not all(isinstance(value, str) for value in values)
                    or values[0] != store
                ):
                    raise ValueError
                position = cast(list[str], values)
                datetime.fromisoformat(position[1])
                uuid.UUID(position[2])
                after = (position[1], position[2])
            except (ValueError, TypeError, binascii.Error) as exc:
                raise ValueError("invalid job cursor for this state directory") from exc
        entries: list[JobSummary] = []
        connection = self.connect()
        try:
            while True:
                rows = connection.execute(
                    "SELECT id, kind, status, scopes_json, submitted_at, started_at, finished_at "
                    "FROM jobs "
                    + ("WHERE (submitted_at, id) < (?, ?) " if after else "")
                    + "ORDER BY submitted_at DESC, id DESC LIMIT 200",
                    after or (),
                ).fetchall()
                for row in rows:
                    summary = self._row_to_summary(row)
                    after = (summary.submitted_at, summary.id)
                    if not allowed(summary):
                        continue
                    if len(entries) == limit:
                        last = entries[-1]
                        token = base64.b64encode(
                            encode_json([store, last.submitted_at, last.id]).encode()
                        ).decode()
                        return JobPage(tuple(entries), token)
                    if summary.status not in _TERMINAL:
                        self.get(summary.id)
                        refreshed = connection.execute(
                            "SELECT id, kind, status, scopes_json, submitted_at, started_at, "
                            "finished_at FROM jobs WHERE id = ?",
                            (summary.id,),
                        ).fetchone()
                        assert refreshed is not None
                        summary = self._row_to_summary(refreshed)
                    entries.append(summary)
                if len(rows) < 200:
                    return JobPage(tuple(entries), None)
        finally:
            connection.close()

    def get(self, job_id: str) -> Job:
        connection = self.connect()
        try:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        finally:
            connection.close()
        if row is None:
            raise JobNotFoundError(f"unknown job: {job_id}")
        return self._reconcile(self._row_to_job(row))

    def read_log(
        self,
        job_id: str,
        stream: Literal["stdout", "stderr"],
        *,
        offset: int = 0,
        limit: int = 64 * 1024,
    ) -> JobLog:
        if offset < 0:
            raise ValueError("offset must be at least 0")
        if not 1 <= limit <= _LOG_LIMIT:
            raise ValueError(f"limit must be between 1 and {_LOG_LIMIT}")
        job = self.get(job_id)
        path = self.directory / job_id / f"{stream}.log"
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                content = handle.read(limit)
        except FileNotFoundError:
            content = b""
        next_offset = offset + len(content)
        size = path.stat().st_size if path.exists() else 0
        return JobLog(
            job_id=job_id,
            stream=stream,
            offset=offset,
            next_offset=next_offset,
            complete=job.status in _TERMINAL and next_offset >= size,
            content=content,
        )

    def cancel(self, job_id: str) -> Job:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFoundError(f"unknown job: {job_id}")
            status = JobStatus(row["status"])
            if status in _TERMINAL:
                return self._row_to_job(row)
            connection.execute("UPDATE jobs SET cancellation_requested = 1 WHERE id = ?", (job_id,))
            connection.commit()
        finally:
            connection.close()
        # The durable request outlives this caller. Only the owning supervisor
        # signals its child; persisted PIDs are never used as signalling authority.
        deadline = time.monotonic() + GRACE_SECONDS + KILL_SECONDS + 2
        while True:
            job = self.get(job_id)
            if job.status in _TERMINAL or time.monotonic() >= deadline:
                return job
            time.sleep(POLL_SECONDS)

    def connect(self) -> sqlite3.Connection:
        connection = self.coordination.connect()
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                scopes_json TEXT NOT NULL,
                request_json TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                config_path TEXT NOT NULL,
                config_fingerprint TEXT NOT NULL,
                payload_path TEXT,
                idempotency_key TEXT UNIQUE,
                submitted_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                pid INTEGER,
                error TEXT,
                result_json TEXT,
                cancellation_requested INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS jobs_discovery ON jobs (submitted_at DESC, id DESC)"
        )
        return connection

    def reconcile_claims(self) -> None:
        connection = self.connect()
        try:
            identities = connection.execute(
                "SELECT job_id FROM lock_operations WHERE job_id IS NOT NULL AND status = 'active'"
            ).fetchall()
        finally:
            connection.close()
        for row in identities:
            self.get(row["job_id"])

    def finish(
        self,
        job_id: str,
        status: JobStatus,
        *,
        result: Mapping[str, object] | None = None,
        error: str | None = None,
        local_termination_verified: bool,
    ) -> bool:
        """Publish a terminal result and settle its claim in one transaction.

        Local termination evidence (including a fenced, unstarted worker) permits
        payload cleanup. It does not prove remote termination: remote failures
        still retain their claims. A cleanup failure does not erase stop evidence.
        """
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or JobStatus(row["status"]) in _TERMINAL:
                return False
            if row["cancellation_requested"] and status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
                return False
            if local_termination_verified and row["payload_path"] is not None:
                try:
                    Path(row["payload_path"]).unlink(missing_ok=True)
                except OSError as exc:
                    detail = f"staged payload cleanup failed: {exc}"
                    error = f"{error}; {detail}" if error else detail
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = ?, result_json = ?, error = ?
                WHERE id = ? AND status NOT IN ('succeeded', 'failed', 'cancelled', 'lost')
                """,
                (
                    status.value,
                    timestamp(),
                    encode_json(dict(result)) if result is not None else None,
                    error,
                    job_id,
                ),
            )
            claim = connection.execute(
                "SELECT * FROM lock_operations WHERE id = ?", (job_id,)
            ).fetchone()
            if claim is not None:
                safe = row["started_at"] is None or (
                    local_termination_verified
                    and (bool(claim["local_only"]) or status is JobStatus.SUCCEEDED)
                )
                self.coordination.finish(
                    connection,
                    job_id,
                    safe=safe,
                    reason=None
                    if safe
                    else "job ended without verified resource execution termination",
                )
            connection.commit()
        finally:
            connection.close()
        return True

    def _reconcile(self, job: Job) -> Job:
        if job.status in _TERMINAL:
            return job
        # A kernel-held lock distinguishes a live owner from a reused PID.
        with (self.directory / job.id / "owner.lock").open("a+b") as owner:
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return job
            connection = self.connect()
            try:
                row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
                assert row is not None
                job = self._row_to_job(row)
            finally:
                connection.close()
            if job.status is JobStatus.STARTING:
                if job.cancellation_requested:
                    self.finish(job.id, JobStatus.CANCELLED, local_termination_verified=True)
                elif datetime.now(UTC) - datetime.fromisoformat(job.submitted_at) >= timedelta(
                    seconds=STARTUP_SECONDS
                ):
                    self.finish(
                        job.id,
                        JobStatus.LOST,
                        local_termination_verified=True,
                        error="job startup handoff expired",
                    )
                else:
                    return job
            elif job.status is JobStatus.RUNNING:
                self.finish(
                    job.id,
                    JobStatus.LOST,
                    local_termination_verified=False,
                    error="job supervisor disappeared; local termination is unverified; "
                    "staged payload retained if present",
                )
            else:
                return job
        return self.get(job.id)

    @staticmethod
    def _row_to_summary(row: sqlite3.Row) -> JobSummary:
        raw_scopes = cast(list[dict[str, str]], json.loads(row["scopes_json"]))
        return JobSummary(
            id=row["id"],
            kind=JobKind(row["kind"]),
            status=JobStatus(row["status"]),
            scopes=tuple(
                JobScope(scope["resource"], Operation(scope["operation"])) for scope in raw_scopes
            ),
            submitted_at=row["submitted_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        raw_scopes = cast(list[dict[str, str]], json.loads(row["scopes_json"]))
        result = json.loads(row["result_json"]) if row["result_json"] is not None else None
        return Job(
            id=row["id"],
            kind=JobKind(row["kind"]),
            status=JobStatus(row["status"]),
            scopes=tuple(
                JobScope(scope["resource"], Operation(scope["operation"])) for scope in raw_scopes
            ),
            submitted_at=row["submitted_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error=row["error"],
            result=cast(dict[str, object] | None, result),
            cancellation_requested=bool(row["cancellation_requested"]),
        )
