"""Owned local job supervision and worker execution; internal process entry point."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from ridge._job_process import (
    GRACE_SECONDS,
    KILL_SECONDS,
    POLL_SECONDS,
    STARTUP_SECONDS,
    encode_json,
    timestamp,
)
from ridge.errors import JobNotFoundError, format_error
from ridge.jobs import JobManager
from ridge.model import JobKind, JobStatus


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
    temporary.write_text(encode_json(outcome))
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
    for signum, seconds in ((signal.SIGTERM, GRACE_SECONDS), (signal.SIGKILL, KILL_SECONDS)):
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
            time.sleep(POLL_SECONDS)
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
                    >= timedelta(seconds=STARTUP_SECONDS)
                ):
                    return 0
                time.sleep(POLL_SECONDS)
        connection = manager.connect()
        try:
            # Claim and expiry use the same predicate, so an arbitrarily late
            # supervisor cannot execute an expired or cancelled submission.
            changed = connection.execute(
                """UPDATE jobs SET status = 'running', started_at = ?, pid = ?
                WHERE id = ? AND status = 'starting' AND cancellation_requested = 0
                AND submitted_at > ?""",
                (
                    timestamp(),
                    os.getpid(),
                    job_id,
                    (datetime.now(UTC) - timedelta(seconds=STARTUP_SECONDS)).isoformat(),
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
                        "ridge._job_runner",
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
                        local_termination_verified=stopped,
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
                            local_termination_verified=False,
                            error="worker finished but local termination unverified; "
                            "staged payload retained if present",
                        )
                        return 1
                    # Completion is serialized against cancellation by finish().
                    if manager.finish(
                        job_id,
                        JobStatus(str(outcome["status"])),
                        local_termination_verified=True,
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
                            local_termination_verified=stopped,
                            error="job worker exited without a result; local termination "
                            + ("verified" if stopped else "unverified"),
                        )
                        return 1
                time.sleep(POLL_SECONDS)
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
                local_termination_verified=stopped,
                error=f"job supervision failed: {exc}; local termination "
                + ("verified" if stopped else "unverified; staged payload retained if present"),
            )
            if not finished and stopped:
                manager.finish(job_id, JobStatus.CANCELLED, local_termination_verified=True)
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
