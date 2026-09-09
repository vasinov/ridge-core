import json
import sys
from pathlib import Path
from typing import Literal

import anyio
import pytest
from mcp import Client, StdioServerParameters
from typer.testing import CliRunner

from ridge import RidgeService
from ridge._scope_wire import read_scope_token
from ridge.cli import app
from ridge.errors import AuthorizationDeniedError
from ridge.mcp import create_server


@pytest.fixture
def config(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "data/input").write_bytes(b"original")
    path = tmp_path / "ridge.yaml"
    path.write_text(
        "resources: {data: {provider: local, root: data}, hidden: {provider: local, root: data}}\n"
        "delegation: {data: [data.read, data.stat]}\n"
    )
    return path


def test_cli_create_bind_reconnect_revoke(config: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    base = ["--config", str(config)]
    created = runner.invoke(
        app,
        [
            *base,
            "scope",
            "create",
            "--grant",
            '{"resource":"data","operations":["data.read","data.stat"]}',
        ],
    )
    assert created.exit_code == 0, created.output
    value = json.loads(created.stdout)
    token = value["token"]
    environment = {"RIDGE_SCOPE_TOKEN": token}
    read = runner.invoke(app, [*base, "read", "data", "input"], env=environment)
    assert read.exit_code == 0 and read.stdout == "original"
    denied = runner.invoke(app, [*base, "write", "data", "input", "--text", "no"], env=environment)
    assert denied.exit_code == 2
    assert (tmp_path / "data/input").read_bytes() == b"original"
    access = runner.invoke(app, [*base, "access", "inspect"], env=environment)
    assert access.exit_code == 0 and json.loads(access.stdout)["scope_id"] == value["scope"]["id"]
    assert token not in access.output
    validation = runner.invoke(app, [*base, "config", "validate", "--json"], env=environment)
    assert validation.exit_code == 2 and "hidden" not in validation.output
    token_file = tmp_path / "token"
    token_file.write_text(token + "\n")
    read = runner.invoke(
        app,
        [*base, "--scope-token-file", str(token_file), "read", "data", "input"],
        env={"RIDGE_SCOPE_TOKEN": "wrong"},
    )
    assert read.exit_code == 0 and read.stdout == "original"
    closed = runner.invoke(app, [*base, "scope", "revoke", value["scope"]["id"]])
    assert closed.exit_code == 0
    denied = runner.invoke(app, [*base, "resources"], env=environment)
    assert denied.exit_code == 2 and "revoked" in denied.output and token not in denied.output


@pytest.mark.parametrize("token", ["", "wrong", "x" * 257])
def test_cli_invalid_binding_never_falls_back(config: Path, token: str) -> None:
    result = CliRunner().invoke(
        app, ["--config", str(config), "resources"], env={"RIDGE_SCOPE_TOKEN": token}
    )
    assert result.exit_code == 2 and "invalid_token" in result.output
    assert "hidden" not in result.output


@pytest.mark.parametrize("content", [b"", b"\n", b"x" * 259, b"\xff"])
def test_invalid_token_file_fails_closed(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "token"
    path.write_bytes(content)
    with pytest.raises(AuthorizationDeniedError):
        read_scope_token(path)


@pytest.fixture
def anyio_backend() -> Literal["asyncio"]:
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_scope_tools_and_separate_stdio_binding(config: Path) -> None:
    operator = RidgeService.from_config(config)
    with anyio.fail_after(20):
        async with Client(create_server(operator)) as parent:
            created = await parent.call_tool(
                "create_scope",
                {"grants": [{"resource": "data", "operations": ["data.read", "data.stat"]}]},
            )
            assert not created.is_error and created.structured_content is not None
            token = created.structured_content["token"]
            identity = created.structured_content["scope"]["id"]
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "ridge.mcp", "--config", str(config)],
                env={"RIDGE_SCOPE_TOKEN": token},
            )
            for _ in range(2):
                async with Client(params) as child:
                    access = await child.call_tool("inspect_access", {})
                    assert not access.is_error and access.structured_content is not None
                    assert access.structured_content["scope_id"] == identity
                    read = await child.call_tool("read_data", {"resource": "data", "path": "input"})
                    assert not read.is_error and read.structured_content is not None
                    assert read.structured_content["content"] == "original"
                    denied = await child.call_tool(
                        "write_data", {"resource": "data", "path": "input", "content": "no"}
                    )
                    assert denied.is_error
            async with Client(params) as child:
                closed = await parent.call_tool("revoke_scope", {"identity": identity})
                assert not closed.is_error
                denied = await child.call_tool("list_resources", {})
                assert denied.is_error and token not in str(denied)
