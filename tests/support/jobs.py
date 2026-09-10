"""Bounded job observation and setup for job lifecycle tests."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ridge import _job_runner as runner
from ridge.application import RidgeService
from ridge.config import load_configuration
from ridge.jobs import JobManager
from ridge.model import JobKind, JobScope, JobStatus, Operation


def wait_for_job(service: RidgeService, job_id: str, *, timeout: float = 5) -> JobStatus:
    """Return any terminal status, leaving the expected outcome to the caller."""
    deadline = time.monotonic() + timeout
    while True:
        job = service.inspect_job(job_id)
        if job.status in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.LOST,
        }:
            return job.status
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"job {job_id} did not finish within {timeout}s: "
                f"status={job.status.value}, error={job.error!r}"
            )
        time.sleep(min(0.02, remaining))


def job_config(tmp_path: Path) -> Path:
    config = tmp_path / "ridge.yaml"
    config.write_text(
        "resources: {local: {provider: local, root: .}}\nstate: {directory: job-state}\n"
    )
    return config


def unstarted_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[JobManager, str]:
    config = job_config(tmp_path)
    manager = JobManager(
        tmp_path / "job-state", config, load_configuration(config).resource_identities
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
