"""Durable, immediate background jobs managed by a local supervisor."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from ridge.coordination import Coordination
from ridge.errors import JobConflictError, JobNotFoundError, RidgeError, format_error
from ridge.model import Job, JobKind, JobLog, JobPage, JobScope, JobStatus, JobSummary, Operation

_TERMINAL = {
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.LOST,
}
_LOG_LIMIT = 1024 * 1024
_STARTUP_SECONDS = 30
_GRACE_SECONDS = 5
_KILL_SECONDS = 5
_POLL_SECONDS = 0.05


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


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
                submitted_at = _now()
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
                            _json([store, last.submitted_at, last.id]).encode()
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
        deadline = time.monotonic() + _GRACE_SECONDS + _KILL_SECONDS + 2
        while True:
            job = self.get(job_id)
            if job.status in _TERMINAL or time.monotonic() >= deadline:
                return job
            time.sleep(_POLL_SECONDS)

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
        cleanup: bool = True,
    ) -> bool:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or JobStatus(row["status"]) in _TERMINAL:
                return False
            if row["cancellation_requested"] and status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
                return False
            if cleanup and row["payload_path"] is not None:
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
                    _now(),
                    _json(dict(result)) if result is not None else None,
                    error,
                    job_id,
                ),
            )
            claim = connection.execute(
                "SELECT * FROM lock_operations WHERE id = ?", (job_id,)
            ).fetchone()
            if claim is not None:
                safe = row["started_at"] is None or (
                    cleanup and (bool(claim["local_only"]) or status is JobStatus.SUCCEEDED)
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
                    self.finish(job.id, JobStatus.CANCELLED)
                elif datetime.now(UTC) - datetime.fromisoformat(job.submitted_at) >= timedelta(
                    seconds=_STARTUP_SECONDS
                ):
                    self.finish(job.id, JobStatus.LOST, error="job startup handoff expired")
                else:
                    return job
            elif job.status is JobStatus.RUNNING:
                self.finish(
                    job.id,
                    JobStatus.LOST,
                    cleanup=False,
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


class _Cancelled(Exception):
    pass


def _load_manager(directory: Path, job_id: str) -> tuple[JobManager, sqlite3.Row]:
    database = directory / "state.sqlite3"
    connection = sqlite3.connect(database, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        connection.close()
    if row is None:
        raise JobNotFoundError(f"unknown job: {job_id}")
    manager = JobManager(
        directory,
        Path(row["config_path"]),
        row["config_fingerprint"],
    )
    return manager, row


def _work(directory: Path, job_id: str, gate: int) -> int:
    # EOF means the supervisor died before authorizing execution. No provider
    # code or user operation runs before this handoff.
    with os.fdopen(gate, "rb") as handoff:
        if handoff.read(1) != b"1":
            return 2
    from ridge._job_process import current_job, in_job_worker

    in_job_worker.set(True)
    current_job.set(job_id)
    manager, row = _load_manager(directory, job_id)

    def cancelled(_signum: int, _frame: object) -> None:
        raise _Cancelled

    signal.signal(signal.SIGTERM, cancelled)
    job_directory = directory / job_id
    stdout_path = job_directory / "stdout.log"
    stderr_path = job_directory / "stderr.log"
    request = cast(dict[str, object], json.loads(row["request_json"]))
    payload_path = Path(row["payload_path"]) if row["payload_path"] is not None else None
    outcome: dict[str, object]
    try:
        if row["cancellation_requested"]:
            raise _Cancelled
        stdout_path.touch(mode=0o600)
        stderr_path.touch(mode=0o600)
        from ridge.application import RidgeService
        from ridge.config import load_configuration

        loaded = load_configuration(
            manager.config_path, expected_fingerprint=manager.config_fingerprint
        )
        service = RidgeService._from_configuration(loaded)  # pyright: ignore[reportPrivateUsage]
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
        outcome = {"status": JobStatus.SUCCEEDED.value, "result": result_value}
    except _Cancelled as exc:
        outcome = {"status": JobStatus.CANCELLED.value}
        if getattr(exc, "__notes__", ()):
            outcome["error"] = format_error(exc)
    except Exception as exc:  # noqa: BLE001 - persist arbitrary provider failures for observers
        message = f"{type(exc).__name__}: {format_error(exc)}"
        with suppress(OSError), stderr_path.open("ab") as handle:
            handle.write((message + "\n").encode(errors="replace"))
        outcome = {"status": JobStatus.FAILED.value, "error": message}
    # Only the supervisor publishes terminal state and cleans staged input.
    temporary = job_directory / "outcome.tmp"
    temporary.write_text(_json(outcome))
    temporary.replace(job_directory / "outcome.json")
    return 0


def _group_members(pgid: int) -> dict[int, str]:
    """Inspect POSIX process state without reaping the owned group leader.

    An unreaped leader pins the group identity during escalation. Zombies have
    stopped executing and cannot produce side effects, even if init reaps slowly.
    Inspection failure is uncertainty, not evidence of termination.
    """
    result = subprocess.run(
        ("ps", "-axo", "pid=,pgid=,stat="),
        capture_output=True,
        text=True,
        check=True,
        timeout=1,
    )
    members: dict[int, str] = {}
    for line in result.stdout.splitlines():
        pid, group, state = line.split()
        if int(group) == pgid:
            members[int(pid)] = state
    return members


def _live_group(pgid: int) -> bool:
    return any(not state.startswith("Z") for state in _group_members(pgid).values())


def _stop_group(process: subprocess.Popen[bytes]) -> bool:
    # Do not poll/wait/reap the leader until all signalling is finished: its PID
    # must not be recycled while it is our process-group signalling target.
    for signum, seconds in ((signal.SIGTERM, _GRACE_SECONDS), (signal.SIGKILL, _KILL_SECONDS)):
        if not _live_group(process.pid):
            process.wait(timeout=1)
            return True
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass
        except PermissionError:
            # macOS may reject a signal when the last live group member exited
            # between inspection and killpg. Verify again; never infer success
            # merely from a signalling error.
            if _live_group(process.pid):
                raise
            process.wait(timeout=1)
            return True
        deadline = time.monotonic() + seconds
        while True:
            if not _live_group(process.pid):
                process.wait(timeout=1)
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(_POLL_SECONDS)
    return False


def _run(directory: Path, job_id: str) -> int:
    manager, _ = _load_manager(directory, job_id)
    with (directory / job_id / "owner.lock").open("a+b") as owner:
        while True:
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                # An observer may briefly hold the lock before our claim; that
                # is not a duplicate supervisor and must not discard the launch.
                _, pending = _load_manager(directory, job_id)
                if pending["status"] != JobStatus.STARTING.value or (
                    datetime.now(UTC) - datetime.fromisoformat(pending["submitted_at"])
                    >= timedelta(seconds=_STARTUP_SECONDS)
                ):
                    return 0
                time.sleep(_POLL_SECONDS)
        connection = manager.connect()
        try:
            # Claim and expiry use the same predicate, so an arbitrarily late
            # supervisor cannot execute an expired or cancelled submission.
            changed = connection.execute(
                """UPDATE jobs SET status = 'running', started_at = ?, pid = ?
                WHERE id = ? AND status = 'starting' AND cancellation_requested = 0
                AND submitted_at > ?""",
                (
                    _now(),
                    os.getpid(),
                    job_id,
                    (datetime.now(UTC) - timedelta(seconds=_STARTUP_SECONDS)).isoformat(),
                ),
            ).rowcount
            connection.commit()
        finally:
            connection.close()
        if not changed:
            return 0
        process: subprocess.Popen[bytes] | None = None
        worker_stopped = False
        try:
            read_gate, write_gate = os.pipe()
            try:
                process = subprocess.Popen(
                    (
                        sys.executable,
                        "-m",
                        "ridge.jobs",
                        "work",
                        str(directory),
                        job_id,
                        str(read_gate),
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    pass_fds=(read_gate,),
                )
                os.close(read_gate)
                read_gate = -1
                os.write(write_gate, b"1")
            finally:
                if read_gate >= 0:
                    os.close(read_gate)
                os.close(write_gate)
            outcome_path = directory / job_id / "outcome.json"
            next_process_check = time.monotonic()
            while True:
                _, row = _load_manager(directory, job_id)
                if row["cancellation_requested"]:
                    stopped = worker_stopped or _stop_group(process)
                    diagnostic: str | None = None
                    if outcome_path.exists():
                        try:
                            cancelled_outcome = cast(
                                dict[str, object], json.loads(outcome_path.read_text())
                            )
                            value = cancelled_outcome.get("error")
                            if isinstance(value, str):
                                diagnostic = value
                        except (OSError, ValueError):
                            diagnostic = "worker recovery diagnostics could not be read"
                    if not stopped:
                        detail = "cancellation termination unverified; staged payload retained if present"
                        diagnostic = f"{detail}; {diagnostic}" if diagnostic else detail
                    manager.finish(
                        job_id,
                        JobStatus.CANCELLED if stopped else JobStatus.LOST,
                        cleanup=stopped,
                        error=diagnostic,
                    )
                    return 0 if stopped else 1
                if outcome_path.exists():
                    outcome = cast(dict[str, object], json.loads(outcome_path.read_text()))
                    worker_stopped = worker_stopped or _stop_group(process)
                    if not worker_stopped:
                        manager.finish(
                            job_id,
                            JobStatus.LOST,
                            cleanup=False,
                            error="worker finished but local termination unverified; "
                            "staged payload retained if present",
                        )
                        return 1
                    # Completion is serialized against cancellation by finish().
                    if manager.finish(
                        job_id,
                        JobStatus(str(outcome["status"])),
                        result=cast(dict[str, object] | None, outcome.get("result")),
                        error=cast(str | None, outcome.get("error")),
                    ):
                        return 0
                elif time.monotonic() >= next_process_check:
                    # Intent/outcome polling stays responsive without spawning
                    # a process-table inspection on every iteration of live work.
                    next_process_check = time.monotonic() + 1
                    members = _group_members(process.pid)
                    if process.pid not in members or members[process.pid].startswith("Z"):
                        # The worker can publish and exit between our first file
                        # check and the process snapshot. Prefer its durable outcome.
                        if outcome_path.exists():
                            continue
                        stopped = worker_stopped = _stop_group(process)
                        manager.finish(
                            job_id,
                            JobStatus.LOST,
                            cleanup=stopped,
                            error="job worker exited without a result; local termination "
                            + ("verified" if stopped else "unverified"),
                        )
                        return 1
                time.sleep(_POLL_SECONDS)
        except Exception as exc:  # noqa: BLE001 - preserve supervision failures
            stopped = worker_stopped or process is None
            if process is not None and not stopped:
                with suppress(Exception):
                    stopped = _stop_group(process)
            _, row = _load_manager(directory, job_id)
            status = (
                JobStatus.CANCELLED
                if stopped and row["cancellation_requested"]
                else (JobStatus.FAILED if stopped else JobStatus.LOST)
            )
            finished = manager.finish(
                job_id,
                status,
                cleanup=stopped,
                error=f"job supervision failed: {exc}; local termination "
                + ("verified" if stopped else "unverified; staged payload retained if present"),
            )
            if not finished and stopped:
                manager.finish(job_id, JobStatus.CANCELLED)
            return 1


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) == 3 and arguments[0] == "run":
        return _run(Path(arguments[1]), arguments[2])
    if len(arguments) == 4 and arguments[0] == "work":
        return _work(Path(arguments[1]), arguments[2], int(arguments[3]))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
