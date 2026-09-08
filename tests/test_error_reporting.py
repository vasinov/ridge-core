from pathlib import Path
from typing import Literal
from unittest.mock import Mock

import pytest
from mcp import Client
from mcp.types import TextContent
from typer.testing import CliRunner

from ridge.application import RidgeService
from ridge.backends.local import LocalResource
from ridge.cli import app
from ridge.errors import TransferError, format_error
from ridge.mcp import create_server
from ridge.registry import ResourceRegistry


@pytest.fixture
def anyio_backend() -> Literal["asyncio"]:
    return "asyncio"


def test_error_text_reserves_bounded_space_for_recovery_notes() -> None:
    error = TransferError("original failure " + "é" * 20000)
    error.add_note("retained backup at target:.ridge-transfer-123/replaced")
    for _ in range(20):
        error.add_note("cleanup failed " + "🦉" * 20000)
    message = format_error(error)
    assert message.startswith("original failure")
    assert "target:.ridge-transfer-123/replaced" in message
    assert "truncated" in message
    assert len(message.encode()) <= 16 * 1024


@pytest.mark.anyio
async def test_cli_and_mcp_preserve_primary_failure_and_cleanup_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = TransferError("publication failed")
    error.add_note("destination cleanup also failed: retained target:.ridge-transfer-123/replaced")
    service = RidgeService(ResourceRegistry([LocalResource("local", tmp_path)]))
    monkeypatch.setattr(RidgeService, "copy", Mock(side_effect=error))
    monkeypatch.setattr("ridge.cli._service", Mock(return_value=service))
    cli = CliRunner().invoke(app, ["copy", "local:source", "local:target"])
    assert cli.exit_code == 2
    async with Client(create_server(service)) as client:
        result = await client.call_tool(
            "copy", {"source": "local:source", "destination": "local:target"}
        )
    assert result.is_error
    text = "\n".join(item.text for item in result.content if isinstance(item, TextContent))
    for message in (cli.output, text):
        assert "publication failed" in message
        assert "destination cleanup also failed" in message
        assert "target:.ridge-transfer-123/replaced" in message


def test_cli_preserves_cleanup_notes_on_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RidgeService(ResourceRegistry([LocalResource("local", tmp_path)]))
    error = KeyboardInterrupt()
    error.add_note("retained target:.ridge-transfer-interrupted")
    monkeypatch.setattr(RidgeService, "copy", Mock(side_effect=error))
    monkeypatch.setattr("ridge.cli._service", Mock(return_value=service))
    result = CliRunner().invoke(app, ["copy", "local:source", "local:target"])
    assert result.exit_code != 0
    assert "retained target:.ridge-transfer-interrupted" in result.output


def test_transfer_keeps_both_finish_failures_and_nested_cleanup_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = LocalResource("source", tmp_path)
    target = LocalResource("target", tmp_path)
    source_stream = Mock()
    source_stream.kind = "file"
    source_stream.read.return_value = b""
    source_stream.finish.side_effect = TransferError("source verification failed")
    target_stream = Mock()
    target_stream.finish.side_effect = TransferError("destination staging failed")
    cleanup = TransferError("abort failed")
    cleanup.add_note("retained multipart upload fixture-123")
    target_stream.abort.side_effect = cleanup
    monkeypatch.setattr(source, "open_transfer_source", Mock(return_value=source_stream))
    monkeypatch.setattr(target, "open_transfer_destination", Mock(return_value=target_stream))
    service = RidgeService(ResourceRegistry([source, target]))
    with pytest.raises(TransferError, match="source verification failed") as caught:
        service.copy("source:input", "target:output")
    assert str(caught.value) == "source verification failed"
    message = format_error(caught.value)
    assert "destination staging failed" in message
    assert "abort failed" in message
    assert "retained multipart upload fixture-123" in message
