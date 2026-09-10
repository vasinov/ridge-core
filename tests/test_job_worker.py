"""Worker configuration validation, startup gates, and persisted outcomes."""

from __future__ import annotations

# These tests deliberately exercise internal crash and ownership boundaries.
# pyright: reportPrivateUsage=false
import json
import os
import signal
from pathlib import Path
from unittest.mock import Mock

import pytest

from ridge import _job_runner as runner
from ridge.application import RidgeService
from ridge.jobs import JobManager
from tests.support.jobs import unstarted_job


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


def test_worker_executes_the_single_configuration_document_it_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, job_id = unstarted_job(tmp_path, monkeypatch)
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


@pytest.mark.parametrize(
    "edit",
    [
        (
            "# harmless comment\nresources:\n  local: {root: ., provider: local}\n"
            "state: {directory: ./job-state}\n"
        ),
        (
            "resources: {local: {provider: local, root: .}, extra: {provider: local, root: .}}\n"
            "state: {directory: job-state}\npermissions: {local: [data.write]}\n"
        ),
        (
            "resources: {local: {provider: local, root: ., lock_key: local}}\n"
            "state: {directory: job-state}\n"
        ),
    ],
)
def test_worker_accepts_irrelevant_configuration_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edit: str
) -> None:
    manager, job_id = unstarted_job(tmp_path, monkeypatch)
    manager.config_path.write_text(edit)
    outcome = _run_worker(manager, job_id)
    assert outcome["status"] == "succeeded", outcome
    assert (tmp_path / "output").read_bytes() == b"staged"


@pytest.mark.parametrize(
    "edit, message",
    [
        ("resources: {local: {provider: local, root: replacement}}", "resource 'local' changed"),
        (
            "resources: {local: {provider: local, root: ., lock_key: new}}",
            "resource 'local' changed",
        ),
        ("resources: {}", "resource 'local' changed"),
        ("resources: {local: {provider: unknown, root: .}}", "resource 'local' changed"),
        ("resources: {local: {provider: local, root: .}}\npermissions: {}", "authorization denied"),
    ],
)
def test_worker_rejects_changed_targets_and_removed_grants_before_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edit: str, message: str
) -> None:
    manager, job_id = unstarted_job(tmp_path, monkeypatch)
    (tmp_path / "replacement").mkdir()
    manager.config_path.write_text(edit + "\nstate: {directory: job-state}\n")
    outcome = _run_worker(manager, job_id)
    assert outcome["status"] == "failed"
    assert message in str(outcome["error"])
    assert not (tmp_path / "output").exists()
    assert not (tmp_path / "replacement/output").exists()


def test_worker_rejects_changed_state_before_initializing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, job_id = unstarted_job(tmp_path, monkeypatch)
    manager.config_path.write_text(
        manager.config_path.read_text().replace("directory: job-state", "directory: new-state")
    )
    outcome = _run_worker(manager, job_id)
    assert outcome["status"] == "failed"
    assert "state directory changed" in str(outcome["error"])
    assert not (tmp_path / "new-state").exists()
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("endpoint", ["source", "destination"])
def test_copy_worker_validates_both_resource_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    for directory in ("source", "destination", "replacement"):
        (tmp_path / directory).mkdir()
    (tmp_path / "source/input").write_bytes(b"original")
    config = tmp_path / "ridge.yaml"
    config.write_text(
        "resources:\n  source: {provider: local, root: source}\n"
        "  destination: {provider: local, root: destination}\nstate: {directory: state}\n"
    )
    service = RidgeService.from_config(config)
    with monkeypatch.context() as patch:
        patch.setattr(runner.subprocess, "Popen", Mock())
        job = service.submit_copy("source:input", "destination:output")
    config.write_text(config.read_text().replace(f"root: {endpoint}", "root: replacement"))
    outcome = _run_worker(service._job_manager(), job.id)
    assert outcome["status"] == "failed"
    assert f"resource '{endpoint}' changed after submission" in str(outcome["error"])
    assert (tmp_path / "source/input").read_bytes() == b"original"
    assert list((tmp_path / "destination").iterdir()) == []
    assert list((tmp_path / "replacement").iterdir()) == []


@pytest.mark.parametrize("cancelled", [False, True])
def test_worker_persists_cleanup_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    from ridge.errors import TransferError

    manager, job_id = unstarted_job(tmp_path, monkeypatch)
    error = runner._Cancelled() if cancelled else TransferError("publication failed")
    error.add_note("retained staging: target:.ridge-transfer-fixture; rollback failed")
    monkeypatch.setattr(RidgeService, "write_data", Mock(side_effect=error))
    outcome = _run_worker(manager, job_id)
    assert outcome["status"] == ("cancelled" if cancelled else "failed")
    assert "retained staging: target:.ridge-transfer-fixture" in str(outcome["error"])
    assert "rollback failed" in str(outcome["error"])


def test_closed_startup_gate_never_executes_work(tmp_path: Path) -> None:
    read_gate, write_gate = os.pipe()
    os.close(write_gate)
    # No config/job files even exist: the worker must exit before loading them.
    assert runner._work(tmp_path, "absent", read_gate) == 2
