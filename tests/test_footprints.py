"""Admission, provider planning and persisted coverage, without external services."""

# pyright: reportPrivateUsage=false
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from ridge import AccessGrant, Footprint, JobScope, Operation, RidgeService
from ridge._access import ScopeAccessError
from ridge._job_process import current_job
from ridge._planning import FootprintPlanner
from ridge.backends.local import LocalResource
from ridge.backends.s3 import S3Resource
from ridge.claims import Claim, conflicts, covers, decode_claims, encode_claims, normalize
from ridge.errors import AuthorizationDeniedError, LockConflictError, LockOwnershipError
from ridge.registry import ResourceRegistry

READ = Operation.DATA_READ
WRITE = Operation.DATA_WRITE


@pytest.mark.parametrize(
    ("left", "right", "overlap"),
    [
        (None, ("a",), True),
        (("a",), ("a", "b"), True),
        (("a",), ("ab",), False),
        (("a/b",), ("a",), False),
        (("a", "b"), ("a", "c"), False),
        (("a",), ("a",), True),
    ],
)
def test_hierarchy(
    left: tuple[str, ...] | None, right: tuple[str, ...] | None, overlap: bool
) -> None:
    a, b = Claim(left, "shared", "d"), Claim(right, "exclusive", "d")
    assert conflicts((a,), (b,)) is overlap
    assert conflicts((b,), (a,)) is overlap
    assert not conflicts((a,), (replace(b, mode="shared"),))
    assert not conflicts((a,), (replace(b, domain="other"),))


def test_normalization_and_coverage_preserve_modes() -> None:
    broad = Claim(None, "shared", "d")
    narrow = Claim(("a",), "exclusive", "d")
    assert normalize((broad, narrow, broad)) == (broad, narrow)
    assert not covers((broad,), (narrow,))
    assert covers((narrow,), (replace(narrow, mode="shared"),))
    assert normalize((broad, narrow, replace(broad, mode="exclusive"))) == (
        replace(broad, mode="exclusive"),
    )
    assert decode_claims(encode_claims((broad, narrow))) == (broad, narrow)


def workspace(tmp_path: Path, *, extra: str = "") -> tuple[Path, RidgeService]:
    path = tmp_path / "ridge.yaml"
    path.write_text(
        "resources:\n"
        "  a: {provider: s3, bucket: fixture, prefix: base, lock_key: shared}\n"
        "  b: {provider: s3, bucket: fixture, prefix: base/child, lock_key: shared}\n"
        + extra
        + "delegation: {a: [data.read, data.write, data.stat, data.delete]}\n"
    )
    return path, RidgeService.from_config(path)


def test_s3_planning_is_pure_literal_and_canonical() -> None:
    a = S3Resource("a", bucket="fixture", prefix="base")
    b = S3Resource("b", bucket="fixture", prefix="base/parent//literal/../child")
    planner = FootprintPlanner(
        ResourceRegistry((a, b)), {"a": "d", "b": "d"}, {"a": ("parent//literal", "../child")}
    )
    assert (
        planner.plan((JobScope("a", WRITE),), ("../key//value",))
        == planner.plan((JobScope("b", WRITE),), ("../key//value",))
        == (Claim(("base/parent//literal/../child/../key//value",), "exclusive", "d"),)
    )
    assert a._client is None and b._client is None
    assert planner.plan((JobScope("a", Operation.DATA_LIST),), ("prefix",)) == (
        Claim(None, "shared", "d"),
    )


def test_distinct_writes_overlap_but_alias_and_list_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, service = workspace(tmp_path)
    other = RidgeService.from_config(path)
    seen: list[str] = []

    def write(resource: S3Resource, key: str, content: bytes) -> None:
        seen.append(resource._full_key(key))
        if key == "child/one":
            entries = service.list_locks()["entries"]
            assert "base/child/one" in str(entries)
            other.write_data("b", "two", b"independent")
            with pytest.raises(LockConflictError):
                other.write_data("b", "one", b"conflicting")
            with pytest.raises(LockConflictError):
                other.list_data("a")
            with pytest.raises(LockConflictError):
                other.acquire_locks([JobScope("a", WRITE)])

    monkeypatch.setattr(S3Resource, "write_object", write)
    service.write_data("a", "child/one", b"first")
    assert seen == ["base/child/one", "base/child/two"]
    assert service.list_locks()["entries"] == []


@pytest.mark.parametrize(
    "extra",
    [
        "  c: {provider: local, root: ., lock_key: shared}\n",
        "  c: {provider: s3, bucket: other, lock_key: shared}\n",
    ],
)
def test_hidden_incompatible_alias_forces_broad_child_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str
) -> None:
    path, operator = workspace(tmp_path, extra=extra)
    issued = operator.create_scope([AccessGrant("a", frozenset({WRITE}), data_root="child")])
    child = RidgeService.from_config(path, scope_token=issued.token)

    def write(_resource: S3Resource, _key: str, _content: bytes) -> None:
        assert '"scope": null' in json.dumps(operator.list_locks())
        with pytest.raises(LockConflictError):
            operator.write_data("b", "different", b"no")

    monkeypatch.setattr(S3Resource, "write_object", write)
    child.write_data("a", "one", b"yes")


def test_child_root_coordinates_survive_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, operator = workspace(tmp_path)
    issued = operator.create_scope([AccessGrant("a", frozenset({WRITE}), data_root="child")])

    def write(_resource: S3Resource, _key: str, _content: bytes) -> None:
        with pytest.raises(LockConflictError):
            operator.write_data("b", "same", b"no")

    monkeypatch.setattr(S3Resource, "write_object", write)
    for _ in range(2):
        child = RidgeService.from_config(path, scope_token=issued.token)
        child.write_data("a", "same", b"yes")


def test_shared_readers_and_exclusive_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, service = workspace(tmp_path)
    other = RidgeService.from_config(path)
    monkeypatch.setattr(S3Resource, "stat_object", Mock(return_value=object()))

    def read(_resource: S3Resource, _key: str, *, max_bytes: int | None = None) -> bytes:
        other.stat_data("b", "same")
        with pytest.raises(LockConflictError):
            other.delete_data("b", "same")
        return b"read"

    monkeypatch.setattr(S3Resource, "read_object", read)
    assert service.read_data("a", "child/same") == b"read"


def test_sessions_remain_broad_while_owned_operations_can_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, service = workspace(tmp_path)
    session = service.acquire_locks([JobScope("a", WRITE)])
    owned = service.with_lock(str(session["token"]))
    assert session["claims"] == [{"domain": "shared", "scope": None, "mode": "exclusive"}]

    def write(_resource: S3Resource, key: str, _content: bytes) -> None:
        if key == "first":
            owned.write_data("a", "second", b"yes")
            with pytest.raises(LockConflictError):
                owned.write_data("a", "first", b"no")
            with pytest.raises(LockConflictError):
                service.write_data("a", "third", b"no")

    monkeypatch.setattr(S3Resource, "write_object", write)
    owned.write_data("a", "first", b"yes")
    service.release_locks(str(session["token"]))


def test_copy_atomic_failure_and_background_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, service = workspace(tmp_path)
    monkeypatch.setattr("ridge.jobs.subprocess.Popen", Mock())
    monkeypatch.setattr(S3Resource, "write_object", Mock())
    occupied = service.submit_write("b", "out", b"held")
    with pytest.raises(LockConflictError):
        service.submit_copy("a:input", "b:out")
    assert len(service.list_jobs().jobs) == 1
    service.write_data("a", "input", b"not partially acquired")
    copy_job = service.submit_copy("a:input", "b:other")
    held = decode_claims(json.dumps(service.inspect_lock(copy_job.id)["claims"]))
    assert set(held) == {
        Claim(("base/input",), "shared", "shared"),
        Claim(("base/child/other",), "exclusive", "shared"),
    }
    service._coordination().validate_job(
        copy_job.id, (JobScope("a", READ), JobScope("b", WRITE)), held
    )
    with pytest.raises(LockOwnershipError):
        service._coordination().validate_job(
            copy_job.id,
            (JobScope("b", WRITE),),
            (Claim(("base/child/different",), "exclusive", "shared"),),
        )
    assert occupied.id != copy_job.id


def test_worker_replanning_fails_before_dispatch_on_new_incompatible_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, service = workspace(tmp_path)
    monkeypatch.setattr("ridge.jobs.subprocess.Popen", Mock())
    backend = Mock()
    monkeypatch.setattr(S3Resource, "write_object", backend)
    job = service.submit_write("a", "key", b"payload")
    path.write_text(
        path.read_text().replace(
            "delegation:", "  c: {provider: local, root: ., lock_key: shared}\ndelegation:"
        )
    )
    worker = RidgeService.from_config(path)
    token = current_job.set(job.id)
    try:
        with pytest.raises(LockOwnershipError):
            worker.write_data("a", "key", b"payload")
    finally:
        current_job.reset(token)
    backend.assert_not_called()


def test_uncertainty_retains_only_original_footprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, service = workspace(tmp_path)
    monkeypatch.setattr(S3Resource, "write_object", Mock(side_effect=OSError("transport")))
    with pytest.raises(OSError):
        service.write_data("a", "child/failed", b"payload")
    monkeypatch.setattr(S3Resource, "write_object", Mock())
    service.write_data("b", "independent", b"yes")
    with pytest.raises(LockConflictError):
        service.write_data("b", "failed", b"no")
    entries = service.list_locks()["entries"]
    assert "uncertain" in str(entries) and "base/child/failed" in str(entries)


def test_revocation_during_pure_planning_is_rechecked_at_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, operator = workspace(tmp_path)
    scope = operator.create_scope([AccessGrant("a", frozenset({WRITE}))])
    child = RidgeService.from_config(path, scope_token=scope.token)
    original = S3Resource.plan_footprint

    def plan(self: S3Resource, operation: Operation, target: str, roots: tuple[str, ...]):
        operator.revoke_scope(scope.scope.id)
        return original(self, operation, target, roots)

    monkeypatch.setattr(S3Resource, "plan_footprint", plan)
    backend = Mock()
    monkeypatch.setattr(S3Resource, "write_object", backend)
    with pytest.raises(ScopeAccessError):
        child.write_data("a", "key", b"no")
    backend.assert_not_called()


def test_authorization_precedes_planning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, _service = workspace(tmp_path)
    path.write_text(path.read_text() + "permissions: {}\n")
    service = RidgeService.from_config(path)
    planner = Mock(side_effect=AssertionError("must not plan"))
    monkeypatch.setattr(S3Resource, "plan_footprint", planner)
    with pytest.raises(AuthorizationDeniedError):
        service.write_data("a", "key", b"no")
    planner.assert_not_called()
    assert not (tmp_path / ".ridge").exists()


def test_bounds_and_invalid_provider_plans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _path, service = workspace(tmp_path)
    for plan in (
        (Footprint(("x",) * 33, "exclusive"),),
        (Footprint(("x" * 16385,), "exclusive"),),
        tuple(Footprint((str(i),), "exclusive") for i in range(65)),
    ):
        monkeypatch.setattr(S3Resource, "plan_footprint", Mock(return_value=plan))
        assert service._planner.plan((JobScope("a", WRITE),), ("key",)) == (
            Claim(None, "exclusive", "shared"),
        )
    invalid_plans: tuple[object, ...] = ((), [], ("bad",), (Footprint(("key",), "shared"),))
    for invalid in invalid_plans:
        monkeypatch.setattr(S3Resource, "plan_footprint", Mock(return_value=invalid))
        with pytest.raises(ValueError):
            service.submit_write("a", "key", b"no")
    if (tmp_path / ".ridge/state.sqlite3").exists():
        with sqlite3.connect(tmp_path / ".ridge/state.sqlite3") as connection:
            assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_filesystems_stay_broad(tmp_path: Path) -> None:
    planner = FootprintPlanner(ResourceRegistry((LocalResource("local", tmp_path),)), {})
    assert planner.plan((JobScope("local", WRITE),), ("a",)) == (Claim(None, "exclusive", "local"),)


def test_invalid_coordinate_rejected_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, service = workspace(tmp_path)
    for coordinate in ((), ("",), "s3", ["s3"], (123,)):
        monkeypatch.setattr(S3Resource, "coordinate_space", Mock(return_value=coordinate))
        with pytest.raises(ValueError, match="coordinate"):
            service.write_data("a", "key", b"no")
    assert not (tmp_path / ".ridge").exists()
