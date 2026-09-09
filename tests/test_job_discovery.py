from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Literal, cast

import anyio
import pytest
from anyio.to_thread import run_sync
from mcp import Client, StdioServerParameters

from ridge import AuthorizationPolicy, JobStatus, Operation, RidgeService
from ridge.backends.local import LocalResource
from ridge.jobs import JobManager
from ridge.registry import ResourceRegistry


def _history(tmp_path: Path, count: int = 405) -> tuple[JobManager, RidgeService]:
    manager = JobManager(tmp_path / "state", tmp_path / "ridge.yaml", {})
    with manager.connect() as connection:
        for index in range(count):
            connection.execute(
                "INSERT INTO jobs (id, kind, status, scopes_json, request_json, request_digest, "
                "config_path, resource_identities_json, submitted_at, cancellation_requested, result_json) "
                "VALUES (?, 'write', 'succeeded', ?, '{}', '', '', '{}', ?, 0, ?)",
                (
                    str(uuid.UUID(int=index + 1)),
                    json.dumps(
                        [
                            {
                                "resource": "local",
                                "operation": "data.write" if index % 100 == 0 else "compute.exec",
                            }
                        ]
                    ),
                    "2026-01-01T00:00:00+00:00",
                    json.dumps({"large": "x" * 10000}),
                ),
            )
    service = RidgeService(
        ResourceRegistry([LocalResource("local", tmp_path)]),
        AuthorizationPolicy.exact({"local": frozenset({Operation.DATA_WRITE})}),
        manager,
    )
    return manager, service


def test_pages_filter_before_filling_and_break_timestamp_ties(tmp_path: Path) -> None:
    manager, service = _history(tmp_path)
    first = service.list_jobs(limit=2)
    assert [job.id for job in first.jobs] == [str(uuid.UUID(int=i)) for i in (401, 301)]
    assert first.next_cursor is not None
    second = service.list_jobs(limit=2, cursor=first.next_cursor)
    last = service.list_jobs(limit=2, cursor=second.next_cursor)
    assert [job.id for job in second.jobs + last.jobs] == [
        str(uuid.UUID(int=i)) for i in (201, 101, 1)
    ]
    assert last.next_cursor is None
    assert set(asdict(first.jobs[0])) == {
        "id",
        "kind",
        "status",
        "scopes",
        "submitted_at",
        "started_at",
        "finished_at",
    }
    assert manager.get(first.jobs[0].id).result == {"large": "x" * 10000}
    assert len(manager.list(allowed=lambda _: True).jobs) == 50
    assert len(manager.list(limit=200, allowed=lambda _: True).jobs) == 200
    assert manager.list(allowed=lambda _: False).next_cursor is None


def test_cursor_survives_growth_anchor_removal_and_rechecks_policy(tmp_path: Path) -> None:
    manager, service = _history(tmp_path, 301)
    first = service.list_jobs(limit=1)
    with manager.connect() as connection:
        connection.execute("DELETE FROM jobs WHERE id = ?", (first.jobs[0].id,))
        connection.execute(
            "UPDATE jobs SET submitted_at = '2027-01-01T00:00:00+00:00' WHERE id = ?",
            (str(uuid.UUID(int=2)),),
        )
    unrestricted = RidgeService(ResourceRegistry([]), jobs=manager)
    following = unrestricted.list_jobs(limit=1, cursor=first.next_cursor)
    assert following.jobs[0].id == str(uuid.UUID(int=300))
    assert unrestricted.list_jobs(limit=1).jobs[0].id == str(uuid.UUID(int=2))
    denied = RidgeService(ResourceRegistry([]), AuthorizationPolicy.exact({}), manager)
    assert denied.list_jobs(cursor=first.next_cursor).jobs == ()
    assert service.list_jobs(limit=1, cursor=first.next_cursor).jobs[0].id == str(
        uuid.UUID(int=201)
    )


@pytest.mark.parametrize("limit", [0, -1, 201, True, 1.5])
def test_invalid_limits(tmp_path: Path, limit: int) -> None:
    _, service = _history(tmp_path, 0)
    with pytest.raises(ValueError, match="job limit"):
        service.list_jobs(limit=limit)


@pytest.mark.parametrize("cursor", ["", "!", "a" * 1025, "e30=", "W10=", "bnVsbA=="])
def test_invalid_cursors(tmp_path: Path, cursor: str) -> None:
    _, service = _history(tmp_path, 0)
    with pytest.raises(ValueError, match="invalid job cursor"):
        service.list_jobs(cursor=cursor)


def test_wrong_store_and_malformed_position(tmp_path: Path) -> None:
    _, service = _history(tmp_path, 201)
    token = service.list_jobs(limit=1).next_cursor
    assert token is not None
    other = JobManager(tmp_path / "other", tmp_path / "ridge.yaml", {})
    with pytest.raises(ValueError, match="invalid job cursor"):
        other.list(cursor=token, allowed=lambda _: True)
    position = json.loads(base64.b64decode(token))
    position[1] = "not-a-timestamp"
    with pytest.raises(ValueError, match="invalid job cursor"):
        service.list_jobs(cursor=base64.b64encode(json.dumps(position).encode()).decode())


def test_terminal_discovery_does_not_decode_results(tmp_path: Path) -> None:
    manager, service = _history(tmp_path, 1)
    with manager.connect() as connection:
        connection.execute("UPDATE jobs SET result_json = 'not-json'")
    assert len(service.list_jobs().jobs) == 1


@pytest.fixture
def anyio_backend() -> Literal["asyncio"]:
    return "asyncio"


@pytest.mark.anyio
async def test_real_cli_and_stdio_mcp_discovery_with_growing_history(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {local: {provider: local, root: .}}\n")
    restricted = tmp_path / "restricted.yaml"
    restricted.write_text(
        "resources: {local: {provider: local, root: .}}\npermissions: {local: [data.write]}\n"
    )
    service = RidgeService.from_config(config)

    def submit(path: str) -> str:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ridge",
                "--config",
                str(config),
                "write",
                "local",
                path,
                "--text",
                path,
                "--background",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        job_id = result.stdout.strip().removeprefix("submitted ")
        deadline = time.monotonic() + 10
        while service.inspect_job(job_id).status is not JobStatus.SUCCEEDED:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert (tmp_path / path).read_text() == path
        return job_id

    ids = [await run_sync(submit, f"output-{i}") for i in range(3)]
    hidden = service.submit_execution("local", [sys.executable, "-c", "print('hidden')"])
    deadline = time.monotonic() + 10
    while service.inspect_job(hidden.id).status is not JobStatus.SUCCEEDED:
        assert time.monotonic() < deadline
        await anyio.sleep(0.02)
    cli = await run_sync(
        lambda: subprocess.run(
            [
                sys.executable,
                "-m",
                "ridge",
                "--config",
                str(restricted),
                "jobs",
                "list",
                "--limit",
                "2",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    )
    first = json.loads(cli.stdout)
    assert [job["id"] for job in first["jobs"]] == ids[::-1][:2]
    assert "result" not in first["jobs"][0]
    new_id = await run_sync(submit, "new-output")
    async with Client(
        StdioServerParameters(
            command=sys.executable, args=["-m", "ridge.mcp", "--config", str(restricted)]
        )
    ) as client:
        result = await client.call_tool("list_jobs", {"limit": 2, "cursor": first["next_cursor"]})
        assert not result.is_error
        page = cast(dict[str, object], result.structured_content)
        assert [job["id"] for job in cast(list[dict[str, object]], page["jobs"])] == ids[:1]
        assert page["next_cursor"] is None
        fresh = await client.call_tool("list_jobs", {"limit": 1})
        assert new_id in str(fresh.structured_content)
        inspected = await client.call_tool("inspect_job", {"job_id": ids[0]})
        assert cast(dict[str, object], inspected.structured_content)["result"] == {
            "bytes_written": 8
        }
        denied = await client.call_tool("inspect_job", {"job_id": hidden.id})
        assert denied.is_error
        invalid = await client.call_tool("list_jobs", {"cursor": "invalid"})
        assert invalid.is_error
