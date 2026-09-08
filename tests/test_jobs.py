from __future__ import annotations

# These tests deliberately exercise internal crash and ownership boundaries.
# pyright: reportPrivateUsage=false
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from ridge import _job_runner as runner
from ridge.application import RidgeService
from ridge.authorization import AuthorizationPolicy
from ridge.backends.local import LocalResource
from ridge.config import load_configuration
from ridge.errors import AuthorizationDeniedError, JobConflictError
from ridge.jobs import JobManager
from ridge.model import JobKind, JobScope, JobStatus, Operation
from ridge.registry import ResourceRegistry


def _config(tmp_path: Path) -> Path:
    config = tmp_path / "ridge.yaml"
    config.write_text(
        "resources: {local: {provider: local, root: .}}\nstate: {directory: job-state}\n"
    )
    return config


def _wait(service: RidgeService, job_id: str, timeout: float = 5) -> JobStatus:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = service.inspect_job(job_id).status
        if status in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.LOST,
        }:
            return status
        time.sleep(0.02)
    raise AssertionError("job did not reach a terminal state")


def test_background_write_stages_input_and_survives_caller_return(tmp_path: Path) -> None:
    service = RidgeService.from_config(_config(tmp_path))

    job = service.submit_write("local", "nested/output.bin", b"ridge\x00")

    assert _wait(service, job.id) is JobStatus.SUCCEEDED
    assert (tmp_path / "nested" / "output.bin").read_bytes() == b"ridge\x00"
    inspected = service.inspect_job(job.id)
    assert inspected.kind is JobKind.WRITE
    assert inspected.scopes == (JobScope("local", Operation.DATA_WRITE),)
    assert inspected.result == {"bytes_written": 6}
    assert not (tmp_path / "job-state" / job.id / "payload.bin").exists()


def test_background_copy_uses_locations_at_execution(tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_text("ridge")
    service = RidgeService.from_config(_config(tmp_path))

    job = service.submit_copy("local:source.txt", "local:copied.txt")

    assert _wait(service, job.id) is JobStatus.SUCCEEDED
    assert (tmp_path / "copied.txt").read_text() == "ridge"
    assert service.inspect_job(job.id).result is not None
    assert job.scopes == (
        JobScope("local", Operation.DATA_READ),
        JobScope("local", Operation.DATA_WRITE),
    )


def test_copy_job_visibility_requires_both_data_scopes(tmp_path: Path) -> None:
    service = RidgeService.from_config(_config(tmp_path))
    (tmp_path / "input").write_bytes(b"scope test")
    job = service.submit_copy("local:input", "local:output")
    assert _wait(service, job.id) is JobStatus.SUCCEEDED
    for grant in [Operation.DATA_READ, Operation.DATA_WRITE]:
        restricted = RidgeService(
            ResourceRegistry([LocalResource("local", tmp_path)]),
            AuthorizationPolicy.exact({"local": frozenset({grant})}),
            JobManager(tmp_path / "job-state", tmp_path / "ridge.yaml", "observation-only"),
        )
        assert restricted.list_jobs().jobs == ()
        with pytest.raises(AuthorizationDeniedError):
            restricted.inspect_job(job.id)


def test_background_execution_persists_result_and_logs(tmp_path: Path) -> None:
    service = RidgeService.from_config(_config(tmp_path))

    job = service.submit_execution(
        "local",
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(3)",
        ],
    )

    assert _wait(service, job.id) is JobStatus.SUCCEEDED
    inspected = service.inspect_job(job.id)
    assert inspected.result is not None
    assert inspected.result["exit_code"] == 3
    assert service.read_job_log(job.id, "stdout").content == b"out\n"
    assert service.read_job_log(job.id, "stderr").content == b"err\n"


def test_local_background_execution_exposes_logs_while_running(tmp_path: Path) -> None:
    service = RidgeService.from_config(_config(tmp_path))
    job = service.submit_execution(
        "local",
        [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(1)"],
    )

    deadline = time.monotonic() + 3
    observed_while_running = False
    while time.monotonic() < deadline:
        current = service.inspect_job(job.id)
        log = service.read_job_log(job.id, "stdout")
        if current.status is JobStatus.RUNNING and log.content == b"ready\n":
            observed_while_running = True
            break
        time.sleep(0.02)

    assert observed_while_running
    assert _wait(service, job.id) is JobStatus.SUCCEEDED


def test_cancel_stops_local_process_group_and_records_terminal_state(tmp_path: Path) -> None:
    service = RidgeService.from_config(_config(tmp_path))
    job = service.submit_execution(
        "local",
        [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
    )
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if service.inspect_job(job.id).status is JobStatus.RUNNING:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("job did not start")

    cancelled = service.cancel_job(job.id)

    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.cancellation_requested


def test_idempotency_key_deduplicates_only_identical_requests(tmp_path: Path) -> None:
    service = RidgeService.from_config(_config(tmp_path))

    first = service.submit_write("local", "output.txt", b"one", idempotency_key="request-1")
    repeated = service.submit_write("local", "output.txt", b"one", idempotency_key="request-1")

    assert repeated.id == first.id
    with pytest.raises(JobConflictError, match="different job request"):
        service.submit_write("local", "output.txt", b"two", idempotency_key="request-1")
    assert _wait(service, first.id) is JobStatus.SUCCEEDED


def test_denied_background_write_does_not_create_job_state(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {data: {provider: local, root: .}}")
    manager = JobManager(tmp_path / "jobs", config, "fixture")
    service = RidgeService(
        ResourceRegistry([LocalResource("data", tmp_path)]),
        AuthorizationPolicy.exact({"data": frozenset({Operation.DATA_READ})}),
        manager,
    )

    with pytest.raises(AuthorizationDeniedError, match="data.write"):
        service.submit_write("data", "output.txt", b"no")

    assert not (tmp_path / "jobs").exists()


def test_job_listing_is_filtered_by_current_underlying_grants(tmp_path: Path) -> None:
    service = RidgeService.from_config(_config(tmp_path))
    job = service.submit_write("local", "output.txt", b"yes")
    assert _wait(service, job.id) is JobStatus.SUCCEEDED

    loaded = load_configuration(tmp_path / "ridge.yaml")
    assert loaded.path is not None
    assert loaded.fingerprint is not None
    assert loaded.state_directory is not None
    hidden = RidgeService(
        loaded.registry,
        AuthorizationPolicy.exact({"local": frozenset()}),
        JobManager(loaded.state_directory, loaded.path, loaded.fingerprint),
    )

    assert hidden.list_jobs().jobs == ()
    with pytest.raises(AuthorizationDeniedError, match="authorization denied for job"):
        hidden.inspect_job(job.id)


def _unstarted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[JobManager, str]:
    config = _config(tmp_path)
    import hashlib

    manager = JobManager(
        tmp_path / "job-state", config, hashlib.sha256(config.read_bytes()).hexdigest()
    )

    def no_spawn(*args: object, **kwargs: object) -> None:
        return None

    with monkeypatch.context() as patch:
        patch.setattr(runner.subprocess, "Popen", no_spawn)
        job = manager.submit(
            JobKind.WRITE,
            (JobScope("local", Operation.DATA_WRITE),),
            {"resource": "local", "path": "output"},
            payload=b"staged",
            idempotency_key="retry",
        )
    return manager, job.id


def _expire(manager: JobManager, job_id: str) -> None:
    with manager.connect() as connection:
        connection.execute(
            "UPDATE jobs SET submitted_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(seconds=31)).isoformat(), job_id),
        )


def _run_worker(manager: JobManager, job_id: str) -> dict[str, object]:
    from ridge._job_process import current_job, in_job_worker

    read_gate, write_gate = os.pipe()
    os.write(write_gate, b"1")
    os.close(write_gate)
    handler = signal.getsignal(signal.SIGTERM)
    old_job = current_job.get()
    old_worker = in_job_worker.get()
    try:
        assert runner._work(manager.directory, job_id, read_gate) == 0
    finally:
        signal.signal(signal.SIGTERM, handler)
        current_job.set(old_job)
        in_job_worker.set(old_worker)
    return json.loads((manager.directory / job_id / "outcome.json").read_text())


def test_worker_executes_the_exact_configuration_bytes_it_fingerprinted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, job_id = _unstarted(tmp_path, monkeypatch)
    (tmp_path / "replacement").mkdir()
    read_bytes = Path.read_bytes
    reads = 0

    def swap_after_read(path: Path) -> bytes:
        nonlocal reads
        content = read_bytes(path)
        if path == manager.config_path:
            reads += 1
            path.write_bytes(content.replace(b"root: .", b"root: replacement"))
        return content

    monkeypatch.setattr(Path, "read_bytes", swap_after_read)
    outcome = _run_worker(manager, job_id)
    assert outcome["status"] == "succeeded"
    assert reads == 1
    assert (tmp_path / "output").read_bytes() == b"staged"
    assert not (tmp_path / "replacement/output").exists()


@pytest.mark.parametrize("cancelled", [False, True])
def test_worker_persists_cleanup_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    from ridge.errors import TransferError

    manager, job_id = _unstarted(tmp_path, monkeypatch)
    error = runner._Cancelled() if cancelled else TransferError("publication failed")
    error.add_note("retained staging: target:.ridge-transfer-fixture; rollback failed")
    monkeypatch.setattr(RidgeService, "write_data", Mock(side_effect=error))
    outcome = _run_worker(manager, job_id)
    assert outcome["status"] == ("cancelled" if cancelled else "failed")
    assert "retained staging: target:.ridge-transfer-fixture" in str(outcome["error"])
    assert "rollback failed" in str(outcome["error"])


def test_supervisor_keeps_worker_diagnostics_when_cancellation_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, job_id = _unstarted(tmp_path, monkeypatch)
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
    manager, job_id = _unstarted(tmp_path, monkeypatch)
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
    manager, job_id = _unstarted(tmp_path, monkeypatch)
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
    manager, job_id = _unstarted(tmp_path, monkeypatch)
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
    manager, job_id = _unstarted(tmp_path, monkeypatch)
    assert manager.cancel(job_id).status is JobStatus.CANCELLED
    assert runner._run(manager.directory, job_id) == 0
    assert not (tmp_path / "output").exists()


def test_live_owner_prevents_reconciliation_and_duplicate_supervision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, job_id = _unstarted(tmp_path, monkeypatch)
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
    manager, job_id = _unstarted(tmp_path, monkeypatch)
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
    manager, job_id = _unstarted(tmp_path, monkeypatch)
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
    manager, job_id = _unstarted(tmp_path, monkeypatch)
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
    manager, job_id = _unstarted(tmp_path, monkeypatch)
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
    manager, job_id = _unstarted(tmp_path, monkeypatch)
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


def test_closed_startup_gate_never_executes_work(tmp_path: Path) -> None:
    read_gate, write_gate = os.pipe()
    os.close(write_gate)
    # No config/job files even exist: the worker must exit before loading them.
    assert runner._work(tmp_path, "absent", read_gate) == 2


def test_interrupted_payload_staging_rolls_back_and_removes_new_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, job_id = _unstarted(tmp_path, monkeypatch)
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


def test_ps_failure_is_not_proof_of_termination(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("ps", 1)

    monkeypatch.setattr(runner.subprocess, "run", unavailable)
    with pytest.raises(subprocess.TimeoutExpired):
        runner._live_group(123)


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
    manager, job_id = _unstarted(tmp_path, monkeypatch)
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


def test_stopped_group_is_reaped_without_signalling(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Mock(spec=subprocess.Popen)
    process.pid = 123
    monkeypatch.setattr(runner, "_live_group", Mock(return_value=False))
    signal_group = Mock(side_effect=AssertionError("must not signal an exited group"))
    monkeypatch.setattr(os, "killpg", signal_group)
    assert runner._stop_group(process)
    process.wait.assert_called_once_with(timeout=1)
    signal_group.assert_not_called()


def test_signal_denial_requires_fresh_termination_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Mock(spec=subprocess.Popen)
    process.pid = 123
    live = Mock(side_effect=[True, False])
    monkeypatch.setattr(runner, "_live_group", live)
    monkeypatch.setattr(os, "killpg", Mock(side_effect=PermissionError("exited concurrently")))
    assert runner._stop_group(process)
    assert live.call_count == 2
    monkeypatch.setattr(runner, "_live_group", Mock(return_value=True))
    with pytest.raises(PermissionError):
        runner._stop_group(process)


def test_cancel_verifies_sigterm_resistant_descendant(tmp_path: Path) -> None:
    service = RidgeService.from_config(_config(tmp_path))
    code = (
        "import os,signal,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print(os.getpid(), flush=True); time.sleep(25)']); "
        "child.wait()"
    )
    job = service.submit_execution("local", [sys.executable, "-c", code])
    pid: int | None = None
    group: int | None = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            content = service.read_job_log(job.id, "stdout").content
            if content.strip():
                pid = int(content.strip())
                break
            time.sleep(0.02)
        assert pid is not None
        group = os.getpgid(pid)
        started = time.monotonic()
        result = service.cancel_job(job.id)
        assert result.status is JobStatus.CANCELLED
        assert result.cancellation_requested
        assert time.monotonic() - started >= 4.5
        assert not runner._live_group(group)
        assert service.read_job_log(job.id, "stdout").complete
    finally:
        if pid is not None:
            # Only the exact test child, and only while still in its owned group.
            try:
                if os.getpgid(pid) == group:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_job_process_entry_point_has_no_import_warning() -> None:
    result = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-m", "ridge._job_runner"],
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 2  # No internal run/work request was supplied.
    assert result.stdout == result.stderr == b""
