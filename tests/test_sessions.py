from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Literal

import pytest
from mcp import Client, StdioServerParameters

from ridge import (
    JobScope,
    LockConflictError,
    LockOwnershipError,
    ManagedMCPSession,
    Operation,
    RidgeService,
)
from ridge.mcp import create_server


@pytest.fixture
def anyio_backend() -> Literal["asyncio"]:
    return "asyncio"


@pytest.fixture
def service(tmp_path: Path) -> RidgeService:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {local: {provider: local, root: .}}")
    return RidgeService.from_config(config)


SCOPES = [JobScope("local", Operation.DATA_WRITE), JobScope("local", Operation.COMPUTE_EXEC)]


def test_python_session_renews_during_pause_and_long_execution(service: RidgeService) -> None:
    with service.lock_session(SCOPES, lease_seconds=1) as session:
        bound = session.service
        time.sleep(1.3)
        with pytest.raises(LockConflictError):
            service.write_data("local", "blocked", b"no")
        result = bound.execute("local", [sys.executable, "-c", "import time; time.sleep(1.3)"])
        assert result.exit_code == 0
        bound.write_data("local", "after", b"yes")
        with pytest.raises(ValueError, match="rebound"):
            bound.with_lock(None)
    with pytest.raises(LockOwnershipError, match="not open"):
        bound.write_data("local", "late", b"no")
    assert service.list_locks()["entries"] == []
    with pytest.raises(LockOwnershipError, match="single-use"), session:
        pass


def test_python_renewal_failure_closes_cached_service_and_exit_reports(
    service: RidgeService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = threading.Event()

    def fail(token: str) -> dict[str, object]:
        failed.set()
        raise OSError("injected renewal failure")

    monkeypatch.setattr(service, "renew_locks", fail)
    with (
        pytest.raises(LockOwnershipError, match="renewal failed"),
        service.lock_session(SCOPES, lease_seconds=1) as session,
    ):
        bound = session.service
        assert failed.wait(2)
        # Join the failing heartbeat so the assertion is independent of scheduling.
        for thread in threading.enumerate():
            if thread.name == "ridge-lease":
                thread.join(timeout=2)
        with pytest.raises(LockOwnershipError, match="renewal failed"):
            bound.write_data("local", "blocked", b"no")
        with pytest.raises(LockOwnershipError, match="renewal failed"):
            bound.submit_execution("local", [sys.executable, "-c", "pass"])
    assert service.list_locks()["entries"] == []


def test_python_body_exception_preserved_and_release_failure_visible(
    service: RidgeService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(token: str) -> dict[str, object]:
        raise OSError("injected release failure")

    monkeypatch.setattr(service, "release_locks", fail)
    with pytest.raises(RuntimeError, match="body") as caught, service.lock_session(SCOPES):
        raise RuntimeError("body")
    assert any("release failed" in note for note in caught.value.__notes__)
    with pytest.raises(LockConflictError):
        service.write_data("local", "blocked", b"no")


@pytest.mark.anyio
async def test_mcp_managed_session_stdio_long_call_and_pause(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {local: {provider: local, root: .}}")
    observer = RidgeService.from_config(config)
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "ridge.mcp", "--config", str(config)]
    )
    async with Client(server) as client:
        async with ManagedMCPSession(client, SCOPES, lease_seconds=1) as session:
            await asyncio.sleep(1.3)
            with pytest.raises(LockConflictError):
                observer.write_data("local", "blocked", b"no")
            result = await session.call_tool(
                "execute",
                {
                    "resource": "local",
                    "argv": [sys.executable, "-c", "import time; time.sleep(1.3)"],
                },
            )
            assert not result.is_error
            result = await session.call_tool(
                "write_data",
                {
                    "resource": "local",
                    "path": "after",
                    "content": "yes",
                },
            )
            assert not result.is_error
            with pytest.raises(ValueError, match="own lock_token"):
                await session.call_tool("write_data", {"lock_token": "override"})
            with pytest.raises(ValueError, match="original MCP client"):
                await session.call_tool("release_locks")
        with pytest.raises(LockOwnershipError, match="not open"):
            await session.call_tool("write_data", {})
    assert (tmp_path / "after").read_text() == "yes"
    assert not (tmp_path / "blocked").exists()
    assert observer.list_locks()["entries"] == []


@pytest.mark.anyio
async def test_mcp_cancellation_releases_session(service: RidgeService) -> None:
    entered = asyncio.Event()
    async with Client(create_server(service)) as client:

        async def workflow() -> None:
            async with ManagedMCPSession(client, SCOPES, lease_seconds=1):
                entered.set()
                await asyncio.sleep(30)

        task = asyncio.create_task(workflow())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.list_locks()["entries"] == []


@pytest.mark.anyio
async def test_mcp_renewal_failure_is_sticky_and_visible(
    service: RidgeService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(token: str) -> dict[str, object]:
        raise LockOwnershipError("injected renewal failure")

    monkeypatch.setattr(service, "renew_locks", fail)
    async with Client(create_server(service)) as client:
        with pytest.raises(LockOwnershipError, match="renewal failed"):
            async with ManagedMCPSession(client, SCOPES, lease_seconds=1) as session:
                await asyncio.sleep(0.6)
                with pytest.raises(LockOwnershipError, match="renewal failed"):
                    await session.call_tool(
                        "write_data",
                        {
                            "resource": "local",
                            "path": "blocked",
                            "content": "no",
                        },
                    )
        assert service.list_locks()["entries"] == []


def test_killed_python_host_stops_renewal(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {local: {provider: local, root: .}}")
    ready = tmp_path / "ready"
    program = """
import sys, time
from pathlib import Path
from ridge import RidgeService, JobScope, Operation
service = RidgeService.from_config(sys.argv[1])
with service.lock_session([JobScope('local', Operation.DATA_WRITE)], lease_seconds=1):
    Path(sys.argv[2]).touch()
    time.sleep(10)
"""
    process = subprocess.Popen([sys.executable, "-c", program, str(config), str(ready)])
    observer = RidgeService.from_config(config)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(0.02)
        assert ready.exists()
        time.sleep(1.2)
        with pytest.raises(LockConflictError):
            observer.write_data("local", "blocked", b"no")
        process.kill()
        process.wait(timeout=5)
        time.sleep(1.2)
        observer.write_data("local", "after", b"yes")
        assert (tmp_path / "after").read_bytes() == b"yes"
        assert not (tmp_path / "blocked").exists()
        assert observer.list_locks()["entries"] == []
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_runnable_mcp_host_example(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {local: {provider: local, root: .}}")
    (tmp_path / "input").write_text("example payload")
    example = Path(__file__).resolve().parents[1] / "examples" / "managed_session.py"
    result = subprocess.run(
        [
            sys.executable,
            str(example),
            "--config",
            str(config),
            "--resource",
            "local",
            "--path",
            "input",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert len(result.stdout.splitlines()) == 3
    assert result.stdout.count("'addressing': 'filesystem'") == 3
    assert (tmp_path / "input").read_text() == "example payload"
    assert RidgeService.from_config(config).list_locks()["entries"] == []


def test_python_heartbeat_start_failure_releases(
    service: RidgeService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(thread: threading.Thread) -> None:
        raise RuntimeError("cannot start thread")

    monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(RuntimeError, match="cannot start thread"), service.lock_session(SCOPES):
        pytest.fail("entry should fail")
    assert service.list_locks()["entries"] == []


def test_python_managed_release_preserves_background_claims(service: RidgeService) -> None:
    with service.lock_session(SCOPES, lease_seconds=1) as session:
        job = session.service.submit_execution(
            "local", [sys.executable, "-c", "import time; time.sleep(0.7)"]
        )
    with pytest.raises(LockConflictError):
        service.write_data("local", "blocked", b"no")
    deadline = time.monotonic() + 5
    while service.list_locks()["entries"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert service.inspect_job(job.id).status.value == "succeeded"
    assert service.list_locks()["entries"] == []


def test_python_missed_deadline_is_sticky(
    service: RidgeService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        pytest.raises(LockOwnershipError, match="deadline"),
        service.lock_session(SCOPES, lease_seconds=300) as session,
    ):
        bound = session.service
        with monkeypatch.context() as patch:
            patch.setattr("ridge.sessions.time.monotonic", lambda: float("inf"))
            with pytest.raises(LockOwnershipError, match="deadline"):
                bound.write_data("local", "blocked", b"no")
        with pytest.raises(LockOwnershipError, match="deadline"):
            session.check()
    assert service.list_locks()["entries"] == []


def test_managed_copy_reserves_both_resources(tmp_path: Path) -> None:
    (tmp_path / "source").mkdir()
    (tmp_path / "destination").mkdir()
    (tmp_path / "source" / "input").write_bytes(b"payload")
    config = tmp_path / "ridge.yaml"
    config.write_text(
        "resources:\n  source: {provider: local, root: source}\n"
        "  destination: {provider: local, root: destination}\n"
    )
    service = RidgeService.from_config(config)
    scopes = [
        JobScope("source", Operation.DATA_READ),
        JobScope("destination", Operation.DATA_WRITE),
    ]
    with service.lock_session(scopes) as session:
        session.service.copy("source:input", "destination:output")
        with pytest.raises(LockConflictError):
            service.write_data("source", "input", b"overwrite")
        with pytest.raises(LockConflictError):
            service.read_data("destination", "output")
    assert (tmp_path / "destination" / "output").read_bytes() == b"payload"
    assert service.list_locks()["entries"] == []


@pytest.mark.anyio
async def test_mcp_release_failure_preserves_body_exception(
    service: RidgeService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(token: str) -> dict[str, object]:
        raise LockOwnershipError("injected release failure")

    monkeypatch.setattr(service, "release_locks", fail)
    async with Client(create_server(service)) as client:
        with pytest.raises(RuntimeError, match="body") as caught:
            async with ManagedMCPSession(client, SCOPES):
                raise RuntimeError("body")
        assert any("release failed" in note for note in caught.value.__notes__)
    with pytest.raises(LockConflictError):
        service.write_data("local", "blocked", b"no")


@pytest.mark.anyio
async def test_mcp_missed_deadline_is_sticky(
    service: RidgeService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with Client(create_server(service)) as client:
        with pytest.raises(LockOwnershipError, match="deadline"):
            async with ManagedMCPSession(client, SCOPES) as session:
                with monkeypatch.context() as patch:
                    patch.setattr("ridge.sessions.time.monotonic", lambda: float("inf"))
                    with pytest.raises(LockOwnershipError, match="deadline"):
                        session.check()
                with pytest.raises(LockOwnershipError, match="deadline"):
                    await session.call_tool("write_data", {})
        assert service.list_locks()["entries"] == []
