"""Pure location validation and real frontend rejection before durable admission."""

# pyright: reportPrivateUsage=false

import sys
from pathlib import Path
from typing import Literal
from unittest.mock import Mock

import anyio
import pytest
from mcp import Client, StdioServerParameters

from ridge.application import RidgeService
from ridge.authorization import AuthorizationRequest
from ridge.backends.local import LocalResource
from ridge.config import load_registry
from ridge.errors import InvalidPathError
from ridge.model import CopyRequest, JobScope, Operation, ResourceLocation
from ridge.registry import ResourceRegistry
from ridge.transfer import validate_copy_locations


def test_object_location_validation_keeps_exact_keys(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {objects: {provider: s3, bucket: fixture-only}}")
    registry = load_registry(config)
    source = ResourceLocation("objects", "a/../same")
    validate_copy_locations(registry, CopyRequest(source, ResourceLocation("objects", "same")))
    with pytest.raises(InvalidPathError):
        validate_copy_locations(registry, CopyRequest(source, source))
    assert not (tmp_path / ".ridge").exists()


@pytest.fixture
def anyio_backend() -> Literal["asyncio"]:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("frontend", ["cli", "mcp"])
@pytest.mark.parametrize("background", [False, True])
async def test_equal_copy_frontends_leave_no_job_or_claim(
    tmp_path: Path, frontend: str, background: bool
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    (root / "same").write_bytes(b"original")
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {local: {provider: local, root: data}}")
    with anyio.fail_after(20):
        if frontend == "cli":
            result = await anyio.run_process(
                [
                    sys.executable,
                    "-c",
                    "from ridge.cli import main; main()",
                    "--config",
                    str(config),
                    "copy",
                    "local:./same",
                    "local:same",
                    *(["--background"] if background else []),
                ],
                check=False,
            )
            assert result.returncode == 2
            assert b"different locations" in result.stderr
            assert not (tmp_path / ".ridge").exists()
            written = await anyio.run_process(
                [
                    sys.executable,
                    "-c",
                    "from ridge.cli import main; main()",
                    "--config",
                    str(config),
                    "write",
                    "local",
                    "after",
                    "--text",
                    "unblocked",
                ]
            )
            assert written.returncode == 0
        else:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-c", "from ridge.mcp import main; main()", "--config", str(config)],
            )
            async with Client(parameters) as client:
                response = await client.call_tool(
                    "copy",
                    {
                        "source": "local:./same",
                        "destination": "local:same",
                        "background": background,
                    },
                )
                assert response.is_error
                assert "different locations" in str(response.content)
                assert not (tmp_path / ".ridge").exists()
                written = await client.call_tool(
                    "write_data", {"resource": "local", "path": "after", "content": "unblocked"}
                )
                assert not written.is_error
    assert (root / "same").read_bytes() == b"original"
    assert (root / "after").read_bytes() == b"unblocked"
    service = RidgeService.from_config(config)
    assert service.list_jobs().jobs == ()
    assert service.list_locks()["entries"] == []


@pytest.mark.parametrize("background", [False, True])
def test_copy_preparation_preserves_authorization_context(tmp_path: Path, background: bool) -> None:
    authorizer = Mock()
    service = RidgeService(ResourceRegistry([LocalResource("local", tmp_path)]), authorizer)
    request, scopes = service._prepare_copy(
        "local:input", ResourceLocation("local", "output"), background=background
    )
    assert request == CopyRequest(
        ResourceLocation("local", "input"), ResourceLocation("local", "output")
    )
    assert scopes == (
        JobScope("local", Operation.DATA_READ),
        JobScope("local", Operation.DATA_WRITE),
    )
    context: dict[str, object] = {"background": True} if background else {}
    assert [call.args[0] for call in authorizer.authorize.call_args_list] == [
        AuthorizationRequest.create(
            "local",
            Operation.DATA_READ,
            {**context, "path": "input", "destination": request.destination},
        ),
        AuthorizationRequest.create(
            "local", Operation.DATA_WRITE, {**context, "path": "output", "source": request.source}
        ),
    ]
    assert not list(tmp_path.iterdir())
