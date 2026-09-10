"""Durable ownership, crash reconciliation, and terminal-state cleanup."""

from __future__ import annotations

# These tests deliberately exercise internal crash and ownership boundaries.
# pyright: reportPrivateUsage=false
import fcntl
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from ridge import _job_runner as runner
from ridge.jobs import JobManager
from ridge.model import JobKind, JobScope, JobStatus, Operation
from tests.support.jobs import unstarted_job


def _expire(manager: JobManager, job_id: str) -> None:
    with manager.connect() as connection:
        connection.execute(
            "UPDATE jobs SET submitted_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(seconds=31)).isoformat(), job_id),
        )


def test_supervisor_keeps_worker_diagnostics_when_cancellation_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, job_id = unstarted_job(tmp_path, monkeypatch)
    launch = subprocess.Popen
    worker_script = r"""
import sys
from ridge import _job_runner as runner
from ridge.application import RidgeService

def cancelled_write(self, *args, **kwargs):
    error = runner._Cancelled()
    error.add_note("destination cleanup failed; retained target:.ridge-transfer-cancel/replaced")
    raise error

RidgeService.write_data = cancelled_write
raise SystemExit(runner._work(runner.Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])))
"""

    def launch_worker(argv: tuple[str, ...], **kwargs: object) -> subprocess.Popen[bytes]:
        if len(argv) > 3 and argv[1:4] == ("-m", "ridge._job_runner", "work"):
            argv = (sys.executable, "-c", worker_script, *argv[4:])
        return launch(argv, **kwargs)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(runner.subprocess, "Popen", launch_worker)
    load_manager = runner._load_manager

    def cancel_after_worker_report(
        directory: Path, identity: str
    ) -> tuple[JobManager, runner.sqlite3.Row]:
        if (directory / identity / "outcome.json").exists():
            with manager.connect() as connection:
                connection.execute(
                    "UPDATE jobs SET cancellation_requested = 1 WHERE id = ?", (identity,)
                )
        return load_manager(directory, identity)

    monkeypatch.setattr(runner, "_load_manager", cancel_after_worker_report)
    assert runner._run(manager.directory, job_id) == 0
    inspected = manager.get(job_id)
    assert inspected.status is JobStatus.CANCELLED
    assert "target:.ridge-transfer-cancel/replaced" in str(inspected.error)
    assert not (tmp_path / "output").exists()
    assert not (manager.directory / job_id / "payload.bin").exists()


def test_expired_start_is_lost_and_late_supervisor_cannot_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, job_id = unstarted_job(tmp_path, monkeypatch)
    assert manager.get(job_id).status is JobStatus.STARTING
    _expire(manager, job_id)
    # Even without a prior observer, the supervisor itself must enforce expiry.
    assert runner._run(manager.directory, job_id) == 0
    assert manager.get(job_id).status is JobStatus.LOST
    assert not (tmp_path / "output").exists()
    assert not (manager.directory / job_id / "payload.bin").exists()
    assert runner._run(manager.directory, job_id) == 0


def test_idempotent_retry_reconciles_expired_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, job_id = unstarted_job(tmp_path, monkeypatch)
    _expire(manager, job_id)
    repeated = manager.submit(
        JobKind.WRITE,
        (JobScope("local", Operation.DATA_WRITE),),
        {"resource": "local", "path": "output"},
        payload=b"staged",
        idempotency_key="retry",
    )
    assert repeated.id == job_id
    assert repeated.status is JobStatus.LOST
    assert not (tmp_path / "output").exists()


def test_discovery_reconciles_only_visible_returned_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, job_id = unstarted_job(tmp_path, monkeypatch)
    _expire(manager, job_id)
    assert manager.list(allowed=lambda _: False).jobs == ()
    with manager.connect() as connection:
        assert connection.execute("SELECT status FROM jobs").fetchone()[0] == "starting"
    page = manager.list(allowed=lambda _: True)
    assert page.jobs[0].status is JobStatus.LOST
    assert page.jobs[0].finished_at is not None
    assert page.next_cursor is None
    assert manager.get(job_id).error == "job startup handoff expired"
    assert not (manager.directory / job_id / "payload.bin").exists()


def test_cancel_before_start_fences_late_supervisor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, job_id = unstarted_job(tmp_path, monkeypatch)
    assert manager.cancel(job_id).status is JobStatus.CANCELLED
    assert runner._run(manager.directory, job_id) == 0
    assert not (tmp_path / "output").exists()


def test_live_owner_prevents_reconciliation_and_duplicate_supervision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, job_id = unstarted_job(tmp_path, monkeypatch)
    _expire(manager, job_id)
    with (manager.directory / job_id / "owner.lock").open("a+b") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        assert manager.get(job_id).status is JobStatus.STARTING
        assert runner._run(manager.directory, job_id) == 0
    assert manager.get(job_id).status is JobStatus.LOST


def test_observer_lock_does_not_discard_supervisor_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, job_id = unstarted_job(tmp_path, monkeypatch)
    with (manager.directory / job_id / "owner.lock").open("a+b") as observer:
        fcntl.flock(observer, fcntl.LOCK_EX)
        supervisor = subprocess.Popen(
            [sys.executable, "-m", "ridge._job_runner", "run", str(manager.directory), job_id]
        )
        time.sleep(0.3)
        assert supervisor.poll() is None
    assert supervisor.wait(timeout=5) == 0
    assert manager.get(job_id).status is JobStatus.SUCCEEDED
    assert (tmp_path / "output").read_bytes() == b"staged"


def test_completed_result_wins_late_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, job_id = unstarted_job(tmp_path, monkeypatch)
    assert manager.finish(
        job_id, JobStatus.SUCCEEDED, local_termination_verified=True, result={"bytes_written": 6}
    )
    original = manager.get(job_id)
    assert manager.cancel(job_id) == original
    assert not original.cancellation_requested


def test_lost_owner_never_signals_persisted_pid_or_deletes_live_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, job_id = unstarted_job(tmp_path, monkeypatch)
    with manager.connect() as connection:
        connection.execute("UPDATE jobs SET status = 'running', pid = ?", (os.getpid(),))

    def forbidden(*args: object) -> None:
        raise AssertionError("must not signal a persisted PID")

    monkeypatch.setattr(os, "killpg", forbidden)
    job = manager.cancel(job_id)
    assert job.status is JobStatus.LOST
    assert job.error and "unverified" in job.error
    assert (manager.directory / job_id / "payload.bin").read_bytes() == b"staged"


def test_cancellation_wins_completion_and_terminal_result_is_immutable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, job_id = unstarted_job(tmp_path, monkeypatch)
    with manager.connect() as connection:
        connection.execute("UPDATE jobs SET cancellation_requested = 1 WHERE id = ?", (job_id,))
    assert not manager.finish(
        job_id, JobStatus.SUCCEEDED, local_termination_verified=True, result={"bytes_written": 6}
    )
    assert (manager.directory / job_id / "payload.bin").exists()
    assert manager.finish(job_id, JobStatus.CANCELLED, local_termination_verified=True)
    original = manager.get(job_id)
    assert not manager.finish(
        job_id, JobStatus.FAILED, local_termination_verified=True, error="late failure"
    )
    assert manager.cancel(job_id) == original


def test_cleanup_failure_is_visible_without_rewriting_operation_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, job_id = unstarted_job(tmp_path, monkeypatch)
    payload = manager.directory / job_id / "payload.bin"
    unlink = Path.unlink

    def deny(path: Path, missing_ok: bool = False) -> None:
        if path == payload:
            raise PermissionError("test cleanup denial")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", deny)
    assert manager.finish(
        job_id, JobStatus.SUCCEEDED, local_termination_verified=True, result={"bytes_written": 6}
    )
    job = manager.get(job_id)
    assert job.status is JobStatus.SUCCEEDED
    assert job.result == {"bytes_written": 6}
    assert job.error and "cleanup failed" in job.error
    assert payload.exists()


def test_interrupted_payload_staging_rolls_back_and_removes_new_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, job_id = unstarted_job(tmp_path, monkeypatch)
    manager.finish(job_id, JobStatus.CANCELLED, local_termination_verified=True)
    before = set(manager.directory.iterdir())
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", Mock(side_effect=KeyboardInterrupt("interrupted staging")))
        with pytest.raises(KeyboardInterrupt):
            manager.submit(
                JobKind.WRITE,
                (JobScope("local", Operation.DATA_WRITE),),
                {"resource": "local", "path": "other"},
                payload=b"other",
            )
    assert set(manager.directory.iterdir()) == before
    assert [job.id for job in manager.list(allowed=lambda _: True).jobs] == [job_id]


@pytest.mark.parametrize("local_only", [False, True])
@pytest.mark.parametrize("verified", [False, True])
@pytest.mark.parametrize("status", [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED])
def test_completion_uses_stop_evidence_not_payload_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_only: bool,
    verified: bool,
    status: JobStatus,
) -> None:
    manager, job_id = unstarted_job(tmp_path, monkeypatch)
    with manager.connect() as connection:
        connection.execute(
            "UPDATE jobs SET status = 'running', started_at = submitted_at WHERE id = ?", (job_id,)
        )
        connection.execute(
            "UPDATE lock_operations SET local_only = ? WHERE id = ?", (local_only, job_id)
        )
    payload = manager.directory / job_id / "payload.bin"
    unlink = Path.unlink

    def deny_payload(path: Path, *, missing_ok: bool = False) -> None:
        if path == payload:
            raise OSError("injected cleanup failure")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", deny_payload)
    assert manager.finish(job_id, status, local_termination_verified=verified)
    job = manager.get(job_id)
    assert job.status is status
    assert payload.read_bytes() == b"staged"
    assert bool(job.error and "cleanup failed" in job.error) is verified
    claim = manager.coordination.inspect(job_id)
    safe = verified and (local_only or status is JobStatus.SUCCEEDED)
    assert claim["status"] == ("released" if safe else "uncertain")
