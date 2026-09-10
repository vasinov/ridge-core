"""Real process cancellation and conservative termination evidence."""

from __future__ import annotations

# These tests deliberately exercise internal crash and ownership boundaries.
# pyright: reportPrivateUsage=false
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from ridge import _job_runner as runner
from ridge.application import RidgeService
from ridge.model import JobStatus
from tests.support.jobs import job_config


def test_cancel_stops_local_process_group_and_records_terminal_state(tmp_path: Path) -> None:
    service = RidgeService.from_config(job_config(tmp_path))
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


def test_ps_failure_is_not_proof_of_termination(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("ps", 1)

    monkeypatch.setattr(runner.subprocess, "run", unavailable)
    with pytest.raises(subprocess.TimeoutExpired):
        runner._live_group(123)


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
    service = RidgeService.from_config(job_config(tmp_path))
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
