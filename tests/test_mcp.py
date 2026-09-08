from __future__ import annotations

import base64
import sys
import time
from pathlib import Path
from typing import Literal, cast

import anyio
import pytest
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult

from ridge.application import RidgeService
from ridge.authorization import AuthorizationPolicy
from ridge.backends.local import LocalResource
from ridge.mcp import create_server
from ridge.model import ObjectEntry, ObjectPage, ObjectStat, Operation, ResourceProperty
from ridge.registry import ResourceRegistry
from ridge.resource import ResourceCapabilities


@pytest.fixture
def anyio_backend() -> Literal["asyncio"]:
    return "asyncio"


def _service(tmp_path: Path) -> RidgeService:
    return RidgeService(ResourceRegistry([LocalResource("local", tmp_path)]))


class _StorageFixture:
    name = "storage"
    provider_name = "fixture"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.capabilities = ResourceCapabilities(storage=self)

    def inspect_properties(self) -> dict[str, ResourceProperty]:
        return {}

    def list_objects(
        self, prefix: str = "", *, cursor: str | None = None, limit: int = 1000
    ) -> ObjectPage:
        del cursor
        keys = [key for key in sorted(self.objects) if key.startswith(prefix)][:limit]
        return ObjectPage(
            entries=tuple(
                ObjectEntry(key, len(self.objects[key]), f"etag-{key}", None) for key in keys
            ),
            next_cursor=None,
        )

    def read_object(self, key: str, *, max_bytes: int | None = None) -> bytes:
        content = self.objects[key]
        if max_bytes is not None and len(content) > max_bytes:
            raise ValueError("object exceeds limit")
        return content

    def write_object(self, key: str, content: bytes) -> None:
        self.objects[key] = content

    def stat_object(self, key: str) -> ObjectStat:
        content = self.objects[key]
        return ObjectStat(key, len(content), f"etag-{key}", None)


def _structured(result: CallToolResult) -> dict[str, object]:
    assert not result.is_error
    assert result.structured_content is not None
    return cast(dict[str, object], result.structured_content)


@pytest.mark.anyio
@pytest.mark.parametrize("content", [b"x" * 131072, b"\xff" * 131072, "é".encode() * 65536])
async def test_mcp_bounds_returned_body_independently_of_provider(content: bytes) -> None:
    class OversizedStorage(_StorageFixture):
        def read_object(self, key: str, *, max_bytes: int | None = None) -> bytes:
            assert max_bytes == 65536
            return content

    storage = OversizedStorage()
    storage.objects["changing"] = b"x"
    async with Client(create_server(RidgeService(ResourceRegistry([storage])))) as client:
        result = _structured(
            await client.call_tool("read_data", {"resource": "storage", "path": "changing"})
        )
    assert result["kind"] == "too_large"
    assert result["size"] == len(content)
    assert result["content"] is None
    assert "copy" in str(result["guidance"])


@pytest.mark.anyio
@pytest.mark.parametrize("fault", ["growth", "provider_overflow"])
async def test_stdio_read_race_then_healthy_copy(tmp_path: Path, fault: str) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {local: {provider: local, root: .}}")
    (tmp_path / "changing").write_bytes(b"x")
    bootstrap = (
        f"FAULT = {fault!r}\n"
        + """
from ridge.backends.local import LocalResource
from ridge.mcp import main
original_read = LocalResource.read
def read(self, path, *, max_bytes=None):
    if path == "changing":
        content = b"x" * 131072
        (self.root / path).write_bytes(content)
        if FAULT == "provider_overflow":
            return content
    return original_read(self, path, max_bytes=max_bytes)
LocalResource.read = read
main()
"""
    )
    parameters = StdioServerParameters(
        command=sys.executable, args=["-c", bootstrap, "--config", str(config)]
    )
    with anyio.fail_after(15):
        async with Client(parameters) as client:
            response = await client.call_tool(
                "read_data", {"resource": "local", "path": "changing"}
            )
            if fault == "growth":
                assert response.is_error
                assert response.content[0].type == "text"
                assert "byte limit" in response.content[0].text
            else:
                result = _structured(response)
                assert result["kind"] == "too_large"
                assert result["content"] is None
                assert result["size"] == 131072
            copied = _structured(
                await client.call_tool(
                    "copy", {"source": "local:changing", "destination": "local:copied"}
                )
            )
            assert copied["mode"] == "completed"
    assert (tmp_path / "changing").read_bytes() == b"x" * 131072
    assert (tmp_path / "copied").read_bytes() == b"x" * 131072
    assert not list(tmp_path.glob(".ridge-transfer-*"))


@pytest.mark.anyio
async def test_server_declares_explicit_tools_and_annotations(tmp_path: Path) -> None:
    async with Client(create_server(_service(tmp_path))) as client:
        listing = await client.list_tools()
        resources = _structured(await client.call_tool("list_resources", {}))

    tools = {tool.name: tool for tool in listing.tools}
    assert set(tools) == {
        "acquire_locks",
        "renew_locks",
        "release_locks",
        "inspect_lock",
        "list_locks",
        "force_release_lock",
        "copy",
        "cancel_job",
        "execute",
        "inspect_job",
        "inspect_resource",
        "list_jobs",
        "list_data",
        "list_resources",
        "read_data",
        "read_job_logs",
        "stat_data",
        "write_data",
        "delete_data",
    }
    assert tools["read_data"].annotations is not None
    assert tools["read_data"].annotations.read_only_hint is True
    assert tools["write_data"].annotations is not None
    assert tools["write_data"].annotations.destructive_hint is True
    assert tools["delete_data"].annotations is not None
    assert tools["delete_data"].annotations.destructive_hint is True
    assert tools["delete_data"].annotations.idempotent_hint is False
    assert tools["execute"].annotations is not None
    assert tools["execute"].annotations.idempotent_hint is False

    local = cast(list[dict[str, object]], resources["resources"])[0]
    assert set(local) == {
        "name",
        "provider",
        "addressing",
        "supports_copy",
        "supported_operations",
        "allowed_operations",
        "background_operations",
    }
    assert local["background_operations"] == []


@pytest.mark.anyio
async def test_mcp_lock_tokens_and_declared_result_schemas(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {local: {provider: local, root: .}}")
    service = RidgeService.from_config(config)
    async with Client(create_server(service)) as client:
        acquired = _structured(
            await client.call_tool(
                "acquire_locks", {"scopes": [{"resource": "local", "operation": "data.write"}]}
            )
        )
        token = acquired["token"]
        blocked = await client.call_tool(
            "write_data", {"resource": "local", "path": "blocked", "content": "no"}
        )
        assert blocked.is_error
        owned = await client.call_tool(
            "write_data",
            {"resource": "local", "path": "owned", "content": "yes", "lock_token": token},
        )
        assert not owned.is_error
        info = _structured(await client.call_tool("inspect_lock", {"identity": acquired["id"]}))
        assert "token" not in info and info["status"] == "open"
        page = _structured(await client.call_tool("list_locks", {"limit": 1}))
        assert len(cast(list[object], page["entries"])) == 1
        released = _structured(await client.call_tool("release_locks", {"token": token}))
        assert released["status"] == "released"
    assert (tmp_path / "owned").read_text() == "yes"
    assert not (tmp_path / "blocked").exists()


@pytest.mark.anyio
async def test_filesystem_tools_bound_reads_and_support_replacement_and_binary(
    tmp_path: Path,
) -> None:
    for index in range(3):
        (tmp_path / f"{index}.txt").write_text(str(index))
    (tmp_path / "large.txt").write_bytes(b"x" * (64 * 1024 + 1))
    (tmp_path / "binary.bin").write_bytes(b"\x00\xff")

    async with Client(create_server(_service(tmp_path))) as client:
        first_page = _structured(
            await client.call_tool("list_data", {"resource": "local", "limit": 2})
        )
        large = _structured(
            await client.call_tool("read_data", {"resource": "local", "path": "large.txt"})
        )
        binary = _structured(
            await client.call_tool("read_data", {"resource": "local", "path": "binary.bin"})
        )
        encoded = base64.b64encode(b"\x00ridge\xff").decode()
        written = _structured(
            await client.call_tool(
                "write_data",
                {
                    "resource": "local",
                    "path": "binary.bin",
                    "content": encoded,
                    "encoding": "base64",
                },
            )
        )

    assert len(cast(list[object], first_page["entries"])) == 2
    assert first_page["next_cursor"] is not None
    assert first_page["addressing"] == "filesystem"
    assert large == {
        "kind": "too_large",
        "size": 64 * 1024 + 1,
        "content": None,
        "guidance": "Use copy to move this content without adding it to model context.",
    }
    assert binary["kind"] == "binary"
    assert written == {
        "mode": "completed",
        "result": {"bytes_written": 7},
        "job": None,
    }
    assert (tmp_path / "binary.bin").read_bytes() == b"\x00ridge\xff"


@pytest.mark.anyio
async def test_execute_bounds_streams_and_expected_errors_are_tool_errors(tmp_path: Path) -> None:
    async with Client(create_server(_service(tmp_path))) as client:
        executed = _structured(
            await client.call_tool(
                "execute",
                {
                    "resource": "local",
                    "argv": [sys.executable, "-c", "import sys; sys.stdout.write('x' * 40000)"],
                },
            )
        )
        failed = await client.call_tool("read_data", {"resource": "missing", "path": "anything"})

    assert executed["mode"] == "completed"
    result = cast(dict[str, object], executed["result"])
    stdout = cast(dict[str, object], result["stdout"])
    assert stdout["size"] == 40000
    assert stdout["truncated"] is True
    assert len(cast(str, stdout["content"])) == 32 * 1024
    assert failed.is_error
    assert "unknown resource: missing" in failed.content[0].text  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_storage_tools_use_object_contract_and_replace_content() -> None:
    storage = _StorageFixture()
    service = RidgeService(ResourceRegistry([storage]))

    async with Client(create_server(service)) as client:
        first_write = _structured(
            await client.call_tool(
                "write_data",
                {"resource": "storage", "path": "out/result.txt", "content": "first"},
            )
        )
        replaced = _structured(
            await client.call_tool(
                "write_data",
                {"resource": "storage", "path": "out/result.txt", "content": "replacement"},
            )
        )
        read = _structured(
            await client.call_tool("read_data", {"resource": "storage", "path": "out/result.txt"})
        )
        listed = _structured(
            await client.call_tool("list_data", {"resource": "storage", "path": "out/"})
        )
        stated = _structured(
            await client.call_tool("stat_data", {"resource": "storage", "path": "out/result.txt"})
        )

    assert first_write == {
        "mode": "completed",
        "result": {"bytes_written": 5},
        "job": None,
    }
    assert replaced == {
        "mode": "completed",
        "result": {"bytes_written": 11},
        "job": None,
    }
    assert read["content"] == "replacement"
    assert len(cast(list[object], listed["entries"])) == 1
    assert stated["addressing"] == "object"
    assert cast(dict[str, object], stated["metadata"])["size"] == 11


@pytest.mark.anyio
async def test_mcp_uses_the_same_authorization_boundary(tmp_path: Path) -> None:
    (tmp_path / "readable.txt").write_text("yes")
    service = RidgeService(
        ResourceRegistry([LocalResource("local", tmp_path)]),
        AuthorizationPolicy.exact({"local": frozenset({Operation.DATA_READ, Operation.DATA_STAT})}),
    )

    async with Client(create_server(service)) as client:
        resources = _structured(await client.call_tool("list_resources", {}))
        read = _structured(
            await client.call_tool("read_data", {"resource": "local", "path": "readable.txt"})
        )
        denied = await client.call_tool(
            "write_data", {"resource": "local", "path": "output.txt", "content": "no"}
        )

    local = cast(list[dict[str, object]], resources["resources"])[0]
    assert local["allowed_operations"] == ["data.read", "data.stat"]
    assert read["content"] == "yes"
    assert denied.is_error
    assert "authorization denied for data.write" in denied.content[0].text  # type: ignore[union-attr]
    assert not (tmp_path / "output.txt").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("source_allowed", [False, True])
@pytest.mark.parametrize("background", [False, True])
async def test_mcp_copy_checks_both_data_grants(
    tmp_path: Path,
    source_allowed: bool,
    background: bool,
) -> None:
    service = RidgeService(
        ResourceRegistry([LocalResource("source", tmp_path), LocalResource("dest", tmp_path)]),
        AuthorizationPolicy.exact(
            {"source": frozenset({Operation.DATA_READ})}
            if source_allowed
            else {"dest": frozenset({Operation.DATA_WRITE})}
        ),
    )
    async with Client(create_server(service)) as client:
        result = await client.call_tool(
            "copy",
            {
                "source": "source:missing",
                "destination": "dest:nested/output",
                "background": background,
            },
        )
    assert result.is_error
    assert result.content[0].type == "text"
    assert (
        f"authorization denied for data.{'write' if source_allowed else 'read'}"
        in result.content[0].text
    )
    assert not (tmp_path / "nested").exists()


@pytest.mark.anyio
async def test_stdio_entrypoint_serves_protocol_without_stdout_noise(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {local: {provider: local, root: .}}")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ridge.mcp", "--config", str(config)],
        cwd=tmp_path,
    )

    async with Client(parameters) as client:
        result = _structured(await client.call_tool("list_resources", {}))

    resources = cast(list[dict[str, object]], result["resources"])
    assert resources[0]["name"] == "local"


@pytest.mark.anyio
async def test_mcp_background_write_returns_handle_and_job_tools_reconnect(
    tmp_path: Path,
) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {local: {provider: local, root: .}}")

    async with Client(create_server(RidgeService.from_config(config))) as client:
        resources = _structured(await client.call_tool("list_resources", {}))
        submitted = _structured(
            await client.call_tool(
                "write_data",
                {
                    "resource": "local",
                    "path": "background.txt",
                    "content": "ridge",
                    "background": True,
                    "idempotency_key": "mcp-write-1",
                },
            )
        )
        assert submitted["mode"] == "submitted"
        job = cast(dict[str, object], submitted["job"])
        job_id = cast(str, job["id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            inspected = _structured(await client.call_tool("inspect_job", {"job_id": job_id}))
            if inspected["status"] == "succeeded":
                break
            await anyio.sleep(0.02)
        else:
            raise AssertionError("MCP job did not complete")
        jobs = _structured(await client.call_tool("list_jobs", {}))

    assert any(item["id"] == job_id for item in cast(list[dict[str, object]], jobs["jobs"]))
    local = cast(list[dict[str, object]], resources["resources"])[0]
    assert local["background_operations"] == ["compute.exec", "data.write", "data.delete"]
    assert (tmp_path / "background.txt").read_text() == "ridge"
