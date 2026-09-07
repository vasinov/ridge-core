from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

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
        "resources: {local: {provider: local, root: .}}\njobs: {directory: job-state}\n"
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
        assert restricted.list_jobs() == ()
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
    assert loaded.jobs_directory is not None
    hidden = RidgeService(
        loaded.registry,
        AuthorizationPolicy.exact({"local": frozenset()}),
        JobManager(loaded.jobs_directory, loaded.path, loaded.fingerprint),
    )

    assert hidden.list_jobs() == ()
    with pytest.raises(AuthorizationDeniedError, match="authorization denied for job"):
        hidden.inspect_job(job.id)
