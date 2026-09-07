"""Durable, immediate background jobs managed by a local supervisor."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from ridge.errors import JobConflictError, JobNotFoundError, RidgeError
from ridge.model import Job, JobKind, JobLog, JobScope, JobStatus, Operation

_TERMINAL = {
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.LOST,
}
_LOG_LIMIT = 1024 * 1024


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


class JobManager:
    """SQLite job metadata plus per-job payload and log files."""

    def __init__(self, directory: Path, config_path: Path, config_fingerprint: str) -> None:
        self.directory = directory
        self.config_path = config_path
        self.config_fingerprint = config_fingerprint
        self.database = directory / "jobs.sqlite3"

    def submit(
        self,
        kind: JobKind,
        scopes: Sequence[JobScope],
        request: Mapping[str, object],
        *,
        payload: bytes | None = None,
        idempotency_key: str | None = None,
    ) -> Job:
        if idempotency_key == "":
            raise ValueError("idempotency_key must be non-empty or None")
        payload_digest = hashlib.sha256(payload).hexdigest() if payload is not None else None
        request_json = _json(dict(request))
        scopes_json = _json(
            [{"resource": scope.resource, "operation": scope.operation.value} for scope in scopes]
        )
        request_digest = hashlib.sha256(
            _json(
                {
                    "kind": kind.value,
                    "scopes": json.loads(scopes_json),
                    "request": json.loads(request_json),
                    "payload_sha256": payload_digest,
                    "config_fingerprint": self.config_fingerprint,
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
                    return self._row_to_job(existing)

            job_id = str(uuid.uuid4())
            job_directory = self.directory / job_id
            job_directory.mkdir(mode=0o700)
            payload_path: str | None = None
            if payload is not None:
                payload_file = job_directory / "payload.bin"
                payload_file.write_bytes(payload)
                payload_file.chmod(0o600)
                payload_path = str(payload_file)
            submitted_at = _now()
            try:
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
            except Exception:
                with suppress(OSError):
                    if payload_path is not None:
                        Path(payload_path).unlink()
                    job_directory.rmdir()
                raise
        finally:
            connection.close()

        try:
            process = subprocess.Popen(
                (sys.executable, "-m", "ridge.jobs", "run", str(self.directory), job_id),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            self.finish(job_id, JobStatus.FAILED, error=f"cannot start job supervisor: {exc}")
            raise RidgeError(f"cannot start job supervisor: {exc}") from exc
        self._set_pid(job_id, process.pid)
        return self.get(job_id)

    def list(self) -> tuple[Job, ...]:
        connection = self.connect()
        try:
            rows = connection.execute("SELECT * FROM jobs ORDER BY submitted_at DESC").fetchall()
        finally:
            connection.close()
        return tuple(self._reconcile(self._row_to_job(row), row["pid"]) for row in rows)

    def get(self, job_id: str) -> Job:
        connection = self.connect()
        try:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        finally:
            connection.close()
        if row is None:
            raise JobNotFoundError(f"unknown job: {job_id}")
        return self._reconcile(self._row_to_job(row), row["pid"])

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
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFoundError(f"unknown job: {job_id}")
            status = JobStatus(row["status"])
            if status in _TERMINAL:
                return self._row_to_job(row)
            connection.execute("UPDATE jobs SET cancellation_requested = 1 WHERE id = ?", (job_id,))
            connection.commit()
            pid = cast(int | None, row["pid"])
        finally:
            connection.close()
        if pid is not None:
            with suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGTERM)
        self.finish(job_id, JobStatus.CANCELLED)
        return self.get(job_id)

    def connect(self) -> sqlite3.Connection:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
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
        return connection

    def _set_pid(self, job_id: str, pid: int) -> None:
        connection = self.connect()
        try:
            connection.execute("UPDATE jobs SET pid = ? WHERE id = ?", (pid, job_id))
            connection.commit()
        finally:
            connection.close()

    def finish(
        self,
        job_id: str,
        status: JobStatus,
        *,
        result: Mapping[str, object] | None = None,
        error: str | None = None,
    ) -> None:
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT payload_path FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = ?, result_json = ?, error = ?
                WHERE id = ? AND status NOT IN ('succeeded', 'failed', 'cancelled', 'lost')
                """,
                (
                    status.value,
                    _now(),
                    _json(dict(result)) if result is not None else None,
                    error,
                    job_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        if row is not None and row["payload_path"] is not None:
            with suppress(OSError):
                Path(row["payload_path"]).unlink()

    def _reconcile(self, job: Job, pid: int | None) -> Job:
        if job.status not in {JobStatus.STARTING, JobStatus.RUNNING} or pid is None:
            return job
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            self.finish(job.id, JobStatus.LOST, error="job supervisor exited without a result")
            return self.get(job.id)
        except PermissionError:
            pass
        return job

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


class _Cancelled(Exception):
    pass


def _run(directory: Path, job_id: str) -> int:
    database = directory / "jobs.sqlite3"
    connection = sqlite3.connect(database, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        connection.close()
    if row is None:
        return 2
    manager = JobManager(
        directory,
        Path(row["config_path"]),
        row["config_fingerprint"],
    )
    try:
        fingerprint = hashlib.sha256(manager.config_path.read_bytes()).hexdigest()
    except OSError as exc:
        manager.finish(job_id, JobStatus.FAILED, error=f"cannot read job configuration: {exc}")
        return 2
    if fingerprint != manager.config_fingerprint:
        manager.finish(job_id, JobStatus.FAILED, error="configuration changed after submission")
        return 2

    def cancelled(_signum: int, _frame: object) -> None:
        raise _Cancelled

    signal.signal(signal.SIGTERM, cancelled)
    connection = manager.connect()
    try:
        changed = connection.execute(
            """
            UPDATE jobs SET status = ?, started_at = ?, pid = ?
            WHERE id = ? AND status = ?
            """,
            (
                JobStatus.RUNNING.value,
                _now(),
                os.getpid(),
                job_id,
                JobStatus.STARTING.value,
            ),
        ).rowcount
        connection.commit()
    finally:
        connection.close()
    if not changed:
        return 0

    job_directory = directory / job_id
    stdout_path = job_directory / "stdout.log"
    stderr_path = job_directory / "stderr.log"
    stdout_path.touch(mode=0o600)
    stderr_path.touch(mode=0o600)
    request = cast(dict[str, object], json.loads(row["request_json"]))
    payload_path = Path(row["payload_path"]) if row["payload_path"] is not None else None
    try:
        from ridge.application import RidgeService

        service = RidgeService.from_config(manager.config_path)
        kind = JobKind(row["kind"])
        if kind is JobKind.EXECUTE:
            argv = cast(list[str], request["argv"])
            with (
                stdout_path.open("ab", buffering=0) as stdout,
                stderr_path.open("ab", buffering=0) as stderr,
            ):
                result = service._execute_to_logs(  # pyright: ignore[reportPrivateUsage]
                    cast(str, request["resource"]),
                    argv,
                    stdout,
                    stderr,
                    cwd=cast(str | None, request.get("cwd")),
                    timeout_seconds=cast(float | None, request.get("timeout_seconds")),
                )
            result_value: dict[str, object] = {
                "argv": list(result.argv),
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
            }
        elif kind is JobKind.WRITE:
            assert payload_path is not None
            payload = payload_path.read_bytes()
            service.write_data(cast(str, request["resource"]), cast(str, request["path"]), payload)
            result_value = {"bytes_written": len(payload)}
        else:
            result = service.copy(cast(str, request["source"]), cast(str, request["destination"]))
            result_value = asdict(result)
        manager.finish(job_id, JobStatus.SUCCEEDED, result=result_value)
        return 0
    except _Cancelled:
        manager.finish(job_id, JobStatus.CANCELLED)
        return 143
    except Exception as exc:  # noqa: BLE001 - persist arbitrary provider failures for observers
        message = f"{type(exc).__name__}: {exc}"
        with stderr_path.open("ab") as handle:
            handle.write((message + "\n").encode(errors="replace"))
        manager.finish(job_id, JobStatus.FAILED, error=message)
        return 1
    finally:
        if payload_path is not None:
            with suppress(OSError):
                payload_path.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 3 or arguments[0] != "run":
        return 2
    return _run(Path(arguments[1]), arguments[2])


if __name__ == "__main__":
    raise SystemExit(main())
