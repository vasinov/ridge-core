from __future__ import annotations

# Exercise durable admission and failure boundaries without external services.
# pyright: reportPrivateUsage=false
import json
import sys
import threading
import time
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ridge.application import RidgeService
from ridge.cli import app
from ridge.config import load_configuration
from ridge.errors import (
    AuthorizationDeniedError,
    ConfigurationError,
    InvalidPathError,
    LockConflictError,
    LockOwnershipError,
    PathNotFoundError,
)
from ridge.model import JobScope, JobStatus, Operation
from tests.support.jobs import wait_for_job


def _service(tmp_path: Path, *, permissions: str = "", extra: str = "") -> RidgeService:
    (tmp_path / "data").mkdir(exist_ok=True)
    config = tmp_path / "ridge.yaml"
    config.write_text(
        "resources:\n  a: {provider: local, root: data}\n"
        "  b: {provider: local, root: data, lock_key: a}\n"
        "  c: {provider: local, root: data}\n" + permissions + extra
    )
    return RidgeService.from_config(config)


def _scope(resource: str = "a", operation: Operation = Operation.DATA_WRITE) -> JobScope:
    return JobScope(resource, operation)


def _token(value: dict[str, object]) -> str:
    return str(value["token"])


def test_default_state_and_alias_configuration(tmp_path: Path) -> None:
    _service(tmp_path)
    loaded = load_configuration(tmp_path / "ridge.yaml")
    assert loaded.state_directory == tmp_path / ".ridge"
    assert loaded.lock_keys == {"a": "a", "b": "a", "c": "c"}
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {a: {provider: local, root: data, lock_key: ../bad}}")
    with pytest.raises(ConfigurationError, match="lock_key"):
        load_configuration(config)


def test_exclusive_aliases_and_independent_resources(tmp_path: Path) -> None:
    service = _service(tmp_path)
    acquired = service.acquire_locks([_scope()])
    other = RidgeService.from_config(tmp_path / "ridge.yaml")
    with pytest.raises(LockConflictError):
        other.write_data("b", "blocked", b"no")
    other.write_data("c", "independent", b"yes")
    service.with_lock(_token(acquired)).write_data("a", "owned", b"yes")
    service.release_locks(_token(acquired))
    other.write_data("b", "released", b"yes")
    assert not (tmp_path / "data" / "blocked").exists()


def test_shared_readers_and_atomic_set_rollback(tmp_path: Path) -> None:
    service = _service(tmp_path)
    one = service.acquire_locks([_scope(operation=Operation.DATA_READ)])
    two = service.acquire_locks([_scope("b", Operation.DATA_STAT)])
    with pytest.raises(LockConflictError):
        service.acquire_locks([_scope("c"), _scope("a")])
    free = service.acquire_locks([_scope("c")])
    for session in (one, two, free):
        service.release_locks(_token(session))


def test_authorize_all_before_state_creation(tmp_path: Path) -> None:
    service = _service(tmp_path, permissions="permissions: {a: [data.read]}\n")
    with pytest.raises(AuthorizationDeniedError):
        service.acquire_locks([_scope(operation=Operation.DATA_READ), _scope("c")])
    assert not (tmp_path / ".ridge").exists()


def test_token_does_not_expand_declared_operations_or_current_grants(tmp_path: Path) -> None:
    service = _service(tmp_path)
    session = service.acquire_locks([_scope()])
    owned = service.with_lock(_token(session))
    with pytest.raises(LockOwnershipError, match="not declared"):
        owned.execute("a", [sys.executable, "-c", "raise SystemExit(99)"])
    restricted = _service(tmp_path, permissions="permissions: {a: [data.read]}\n")
    with pytest.raises(AuthorizationDeniedError):
        restricted.with_lock(_token(session)).write_data("a", "no", b"no")
    with pytest.raises(AuthorizationDeniedError):
        restricted.renew_locks(_token(session))
    assert restricted.release_locks(_token(session))["status"] == "released"


def test_expiry_cannot_revive_and_tokens_are_not_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    session = service.acquire_locks([_scope()], lease_seconds=10)
    token = _token(session)
    assert token.encode() not in (tmp_path / ".ridge" / "state.sqlite3").read_bytes()
    future = float(cast(float, session["expires_at"])) + 1
    monkeypatch.setattr("ridge.coordination.time.time", lambda: future)
    with pytest.raises(LockOwnershipError, match="expired"):
        service.renew_locks(token)
    service.write_data("a", "new", b"new")
    assert service.inspect_lock(str(session["id"]))["status"] == "released"


def test_closing_session_retains_entire_set_and_serializes_own_calls(tmp_path: Path) -> None:
    service = _service(tmp_path)
    session = service.acquire_locks([_scope(), _scope("c")])
    coord = service._coordination()
    with coord.operation([_scope()], token=_token(session), local_only=True):
        with pytest.raises(LockConflictError):
            service.with_lock(_token(session)).write_data("a", "no", b"no")
        assert service.release_locks(_token(session))["status"] == "closing"
        with pytest.raises(LockConflictError):
            service.write_data("c", "no", b"no")
        with pytest.raises(LockOwnershipError):
            service.with_lock(_token(session)).write_data("c", "no", b"no")
    assert service.inspect_lock(str(session["id"]))["status"] == "released"


def test_renew_and_expiry_while_active(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    session = service.acquire_locks([_scope()], lease_seconds=10)
    initial = cast(float, session["expires_at"])
    monkeypatch.setattr("ridge.coordination.time.time", lambda: initial - 1)
    renewed = service.renew_locks(_token(session))
    assert cast(float, renewed["expires_at"]) > initial
    with service._coordination().operation([_scope()], token=_token(session), local_only=True):
        monkeypatch.setattr("ridge.coordination.time.time", lambda: initial + 100)
        with pytest.raises(LockConflictError):
            service.write_data("a", "no", b"no")
    service.write_data("a", "yes", b"yes")


def test_uncertain_foreground_requires_deliberate_authorized_recovery(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with (
        pytest.raises(KeyboardInterrupt),
        service._coordination().operation([_scope()], token=None, local_only=True),
    ):
        raise KeyboardInterrupt
    entries = cast(list[dict[str, object]], service.list_locks()["entries"])
    assert len(entries) == 1 and entries[0]["status"] == "uncertain"
    identity = str(entries[0]["id"])
    with pytest.raises(LockConflictError):
        service.write_data("a", "no", b"no")
    with pytest.raises(ValueError):
        service.force_release_lock(identity, reason="")
    restricted = _service(tmp_path, permissions="permissions: {}\n")
    assert restricted.list_locks()["entries"] == []
    with pytest.raises(AuthorizationDeniedError):
        restricted.force_release_lock(identity, reason="no grant")
    released = service.force_release_lock(identity, reason="verified owned work is stopped")
    assert released["status"] == "released"
    assert "verified owned work" in str(released["reason"])
    service.write_data("a", "yes", b"yes")


def test_known_local_data_error_releases_claim(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(PathNotFoundError):
        service.read_data("a", "missing")
    service.write_data("a", "missing", b"created")


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("source", ["same", "./same", "nested/../same"])
def test_equal_copy_rejected_before_claim_or_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, background: bool, source: str
) -> None:
    service = _service(tmp_path)
    endpoint = Mock(side_effect=AssertionError("endpoint must not open"))
    monkeypatch.setattr("ridge.backends.local.LocalResource.open_transfer_source", endpoint)
    monkeypatch.setattr("ridge.backends.local.LocalResource.open_transfer_destination", endpoint)
    with pytest.raises(InvalidPathError, match="different locations"):
        (service.submit_copy if background else service.copy)(f"a:{source}", "a:same")
    endpoint.assert_not_called()
    assert not (tmp_path / ".ridge").exists()
    service.write_data("a", "after-rejection", b"unblocked")
    assert (tmp_path / "data/after-rejection").read_bytes() == b"unblocked"
    assert service.list_jobs().jobs == ()


@pytest.mark.parametrize("background", [False, True])
def test_copy_authorization_precedes_location_validation(tmp_path: Path, background: bool) -> None:
    service = _service(tmp_path, permissions="permissions: {a: [data.read]}\n")
    with pytest.raises(AuthorizationDeniedError):
        (service.submit_copy if background else service.copy)("a:same", "a:same")
    assert not (tmp_path / ".ridge").exists()


def test_copy_failure_after_dispatch_still_retains_claim(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(PathNotFoundError):
        service.copy("a:missing", "c:output")
    entries = cast(list[dict[str, object]], service.list_locks()["entries"])
    assert len(entries) == 1 and entries[0]["status"] == "uncertain"
    with pytest.raises(LockConflictError):
        service.write_data("c", "output", b"blocked")
    service.write_data("c", "unrelated", b"independent")
    assert not (tmp_path / "data/output").exists()


def test_background_submission_and_claim_are_atomic_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr("ridge.jobs.subprocess.Popen", Mock())
    session = service.acquire_locks([_scope()])
    with pytest.raises(LockConflictError):
        service.submit_write("a", "no", b"no")
    assert service.list_jobs().jobs == ()
    owned = service.with_lock(_token(session))
    job = owned.submit_write("a", "yes", b"yes", idempotency_key="one")
    assert owned.submit_write("a", "yes", b"yes", idempotency_key="one").id == job.id
    assert service.inspect_lock(job.id)["job_id"] == job.id
    assert service.release_locks(_token(session))["status"] == "closing"
    service._job_manager().finish(job.id, JobStatus.CANCELLED, local_termination_verified=True)
    assert service.inspect_lock(job.id)["status"] == "released"
    assert service.inspect_lock(str(session["id"]))["status"] == "released"


def test_background_completion_under_closed_session(tmp_path: Path) -> None:
    service = _service(tmp_path)
    session = service.acquire_locks([_scope()])
    job = service.with_lock(_token(session)).submit_write("a", "background", b"yes")
    service.release_locks(_token(session))
    assert wait_for_job(service, job.id) is JobStatus.SUCCEEDED
    assert service.inspect_lock(job.id)["status"] == "released"
    service.write_data("b", "after", b"yes")


def test_fenced_startup_releases_but_lost_running_job_retains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr("ridge.jobs.subprocess.Popen", Mock())
    job = service.submit_write("a", "no", b"no")
    manager = service._job_manager()
    connection = manager.connect()
    connection.execute(
        "UPDATE jobs SET submitted_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (job.id,)
    )
    connection.commit()
    connection.close()
    assert service.inspect_job(job.id).status is JobStatus.LOST
    assert service.inspect_lock(job.id)["status"] == "released"
    lost = service.submit_write("a", "lost", b"no")
    connection = manager.connect()
    connection.execute(
        "UPDATE jobs SET status = 'running', started_at = submitted_at WHERE id = ?", (lost.id,)
    )
    connection.commit()
    connection.close()
    assert service.inspect_job(lost.id).status is JobStatus.LOST
    assert service.inspect_lock(lost.id)["status"] == "uncertain"


def test_remote_cancellation_is_not_resource_termination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr("ridge.jobs.subprocess.Popen", Mock())
    manager = service._job_manager()
    job = service.submit_write("a", "no", b"no")
    connection = manager.connect()
    connection.execute(
        "UPDATE jobs SET status = 'running', started_at = submitted_at WHERE id = ?", (job.id,)
    )
    connection.execute("UPDATE lock_operations SET local_only = 0 WHERE id = ?", (job.id,))
    connection.commit()
    connection.close()
    manager.finish(job.id, JobStatus.CANCELLED, local_termination_verified=True)
    assert service.inspect_lock(job.id)["status"] == "uncertain"


def test_copy_combines_aliases_and_obeys_both_scopes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    (tmp_path / "data" / "input").write_bytes(b"copy")
    session = service.acquire_locks([_scope("a", Operation.DATA_READ), _scope("b")])
    assert session["claims"] == [{"domain": "a", "scope": None, "mode": "exclusive"}]
    service.with_lock(_token(session)).copy("a:input", "b:output")
    assert (tmp_path / "data" / "output").read_bytes() == b"copy"


def test_bounded_wait_can_acquire_after_release(tmp_path: Path) -> None:
    service = _service(tmp_path)
    session = service.acquire_locks([_scope()])
    thread = threading.Thread(
        target=lambda: (time.sleep(0.1), service.release_locks(_token(session)))
    )
    thread.start()
    acquired = service.acquire_locks([_scope()], wait_seconds=2)
    thread.join()
    service.release_locks(_token(acquired))


def test_lock_listing_is_bounded_and_filters_before_filling(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for resource in ("a", "b", "c"):
        service.acquire_locks([_scope(resource, Operation.DATA_READ)])
    restricted = _service(tmp_path, permissions="permissions: {c: [data.read]}\n")
    page = restricted.list_locks(limit=1)
    entries = cast(list[dict[str, object]], page["entries"])
    assert len(entries) == 1 and entries[0]["scopes"] == [
        {"resource": "c", "operation": "data.read"}
    ]
    assert restricted.list_locks(cursor=str(page["next_cursor"]))["entries"] == []


@pytest.mark.parametrize(
    "lease,wait", [(0, 0), (3601, 0), (float("nan"), 0), (300, -1), (300, 61), (300, float("inf"))]
)
def test_invalid_lease_and_wait_rejected(tmp_path: Path, lease: float, wait: float) -> None:
    service = _service(tmp_path)
    with pytest.raises(ValueError):
        service.acquire_locks([_scope()], lease_seconds=lease, wait_seconds=wait)
    assert service.list_locks()["entries"] == []


def test_different_configs_share_only_explicit_state_and_keys(tmp_path: Path) -> None:
    service = _service(tmp_path)
    session = service.acquire_locks([_scope()])
    config = tmp_path / "other.yaml"
    config.write_text("resources: {other: {provider: local, root: data, lock_key: a}}\n")
    shared = RidgeService.from_config(config)
    with pytest.raises(LockConflictError):
        shared.write_data("other", "no", b"no")
    config.write_text(config.read_text() + "state: {directory: separate}\n")
    independent = RidgeService.from_config(config)
    independent.write_data("other", "outside-coordination", b"yes")
    service.release_locks(_token(session))


def test_rejected_calls_do_not_accumulate_owner_files(tmp_path: Path) -> None:
    service = _service(tmp_path)
    session = service.acquire_locks([_scope()])
    for _ in range(3):
        with pytest.raises(LockConflictError):
            service.write_data("a", "no", b"no")
    assert list((tmp_path / ".ridge" / "operations").iterdir()) == []
    service.release_locks(_token(session))


def test_same_yaml_at_different_paths_is_not_same_idempotent_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr("ridge.jobs.subprocess.Popen", Mock())
    config = tmp_path / "duplicate.yaml"
    config.write_bytes((tmp_path / "ridge.yaml").read_bytes())
    service.submit_write("a", "one", b"one", idempotency_key="shared-key")
    from ridge.errors import JobConflictError

    with pytest.raises(JobConflictError):
        RidgeService.from_config(config).submit_write(
            "a", "one", b"one", idempotency_key="shared-key"
        )


def test_cli_session_token_round_trip(tmp_path: Path) -> None:
    _service(tmp_path)
    runner = CliRunner()
    prefix = ["--config", str(tmp_path / "ridge.yaml")]
    result = runner.invoke(app, [*prefix, "locks", "acquire", "a:data.write"])
    assert result.exit_code == 0, result.output
    token = json.loads(result.output)["token"]
    assert runner.invoke(app, [*prefix, "write", "a", "blocked", "--text", "no"]).exit_code == 2
    assert (
        runner.invoke(
            app, [*prefix, "--lock-token", token, "write", "a", "owned", "--text", "yes"]
        ).exit_code
        == 0
    )
    assert runner.invoke(app, [*prefix, "--lock-token", token, "locks", "release"]).exit_code == 0
    assert runner.invoke(app, [*prefix, "write", "a", "free", "--text", "yes"]).exit_code == 0
