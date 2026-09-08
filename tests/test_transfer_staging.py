"""Inspect real staging artifacts across ordinary interruption and uncertain stops."""

# pyright: reportPrivateUsage=false
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO, Literal, cast

import anyio
import pytest
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult

from ridge.backends._transfer import ProcessTransferDestination
from ridge.backends._transfer_helper import TRANSFER_HELPER_SOURCE
from ridge.backends.local import LocalResource
from ridge.errors import ResourceUnavailableError, TransferError, format_error
from ridge.model import CopyRequest, ResourceLocation, TransferPayloadKind
from ridge.registry import ResourceRegistry
from ridge.resource import TransferSource
from ridge.transfer import copy


class InterruptedSource(LocalResource):
    def __init__(self, root: Path, chunks: int, interrupt: bool) -> None:
        super().__init__("source", root)
        self.chunks = chunks
        self.interrupt = interrupt

    def open_transfer_source(self, path: str) -> TransferSource:
        source = super().open_transfer_source(path)
        remaining = self.chunks
        interrupt = self.interrupt

        class Stream:
            kind: TransferPayloadKind = source.kind

            def read(self, size: int) -> bytes:
                nonlocal remaining
                if remaining == 0:
                    if interrupt:
                        raise KeyboardInterrupt("injected interruption")
                    raise TransferError("injected source failure")
                remaining -= 1
                return source.read(size)

            def finish(self) -> None:
                source.finish()

            def cancel(self) -> None:
                source.cancel()

        return Stream()


@pytest.mark.parametrize("kind", ["file", "tree"])
@pytest.mark.parametrize("chunks", [0, 4])
@pytest.mark.parametrize("interrupt", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_source_interruption_removes_stage_and_only_created_empty_parents(
    tmp_path: Path, kind: str, chunks: int, interrupt: bool, existing: bool
) -> None:
    source = tmp_path / "source"
    if kind == "tree":
        source.mkdir()
        source = source / "data"
    source.write_bytes(b"x" * 524288)
    destination = tmp_path / "nested" / "output"
    if existing:
        destination.parent.mkdir()
        if kind == "tree":
            destination.mkdir()
            (destination / "old").write_bytes(b"original")
        else:
            destination.write_bytes(b"original")
    registry = ResourceRegistry(
        [InterruptedSource(tmp_path, chunks, interrupt), LocalResource("target", tmp_path)]
    )
    with pytest.raises(KeyboardInterrupt if interrupt else TransferError) as caught:
        copy(
            registry,
            CopyRequest(
                ResourceLocation("source", "source"), ResourceLocation("target", "nested/output")
            ),
        )
    assert "injected" in str(caught.value)
    assert source.read_bytes() == b"x" * 524288
    assert not list(tmp_path.glob("**/.ridge-transfer-*"))
    if existing:
        assert (destination / "old" if kind == "tree" else destination).read_bytes() == b"original"
    else:
        assert not destination.parent.exists()


@pytest.mark.parametrize("kind", ["file", "tree"])
def test_ready_stage_refuses_abort_and_commit_until_writer_stops(
    tmp_path: Path, kind: TransferPayloadKind
) -> None:
    stream = LocalResource("target", tmp_path).open_transfer_destination("new/output", kind)
    assert isinstance(stream, ProcessTransferDestination)
    (stage,) = tmp_path.glob("new/.ridge-transfer-*")
    assert stream._token == stage.relative_to(tmp_path).as_posix()
    assert json.loads((stage / ".ridge-transfer.json").read_text())["phase"] == "receiving"
    with pytest.raises(TransferError, match="not finished"):
        stream.commit()
    with pytest.raises(TransferError, match="unconfirmed staging"):
        stream.abort()
    response = subprocess.run(
        [sys.executable, "-c", TRANSFER_HELPER_SOURCE, str(tmp_path), "abort"],
        input=json.dumps({"token": stream._token}).encode() + b"\n",
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert response.returncode == 2
    assert stage.exists()
    try:
        stream.cancel()
    except TransferError:
        assert kind == "tree"  # Empty input is not a valid tree archive.
    with pytest.raises(TransferError, match="not finished"):
        stream.commit()
    stream.abort()
    assert not (tmp_path / "new").exists()


def staging_fault_source(fault: str) -> str:
    return (
        f"FAULT = {fault!r}\n"
        + r"""
import sys
import time
import shutil
import os
from pathlib import Path
if sys.argv[2].startswith("stage-"):
    original_write = sys.stdout.write
    def write(text):
        if '"ready":true' in text and FAULT == "startup_crash":
            os._exit(3)
        if '"stopped":true' in text:
            if FAULT == "lost_report":
                return len(text)
            if FAULT == "wrong_token":
                text = text.replace('"token":"', '"token":"wrong-')
        result = original_write(text)
        return result
    sys.stdout.write = write
    if FAULT == "metadata":
        original_replace = Path.replace
        def replace(path, target):
            if '"phase":"receiving"' not in path.read_text():
                raise OSError("injected phase recording failure")
            return original_replace(path, target)
        Path.replace = replace
    if FAULT == "stall":
        original_input = sys.stdin
        class Input:
            @property
            def buffer(self):
                return self
            def readline(self):
                return original_input.buffer.readline()
            def read(self, size=-1):
                time.sleep(30)
                return original_input.buffer.read(size)
        sys.stdin = Input()
if sys.argv[2] == "abort" and FAULT == "cleanup":
    def remove(*args, **kwargs):
        raise OSError("injected staging cleanup failure")
    shutil.rmtree = remove
"""
        + TRANSFER_HELPER_SOURCE
    )


class FaultyDestination(LocalResource):
    def __init__(self, root: Path, fault: str) -> None:
        super().__init__("target", root)
        self.fault = fault

    def _transfer_command(self, operation: str) -> tuple[str, ...]:
        return (sys.executable, "-c", staging_fault_source(self.fault), str(self.root), operation)


@pytest.mark.parametrize("fault", ["lost_report", "wrong_token", "stall"])
def test_unconfirmed_staging_is_retained_and_reported(tmp_path: Path, fault: str) -> None:
    stream = FaultyDestination(tmp_path, fault).open_transfer_destination("new/output", "file")
    assert isinstance(stream, ProcessTransferDestination)
    (stage,) = tmp_path.glob("new/.ridge-transfer-*")
    started = time.monotonic()
    with pytest.raises(TransferError):
        stream.cancel()
    assert time.monotonic() - started < 6
    assert stream.process.poll() is not None
    with pytest.raises(TransferError, match="unconfirmed staging") as caught:
        stream.abort()
    assert stage.name in format_error(caught.value)
    assert stage.exists()
    assert not (tmp_path / "new/output").exists()


def test_cleanup_failure_preserves_primary_and_identifies_stage(tmp_path: Path) -> None:
    (tmp_path / "source").write_bytes(b"source")
    registry = ResourceRegistry(
        [InterruptedSource(tmp_path, 0, False), FaultyDestination(tmp_path, "cleanup")]
    )
    with pytest.raises(TransferError, match="injected source failure") as caught:
        copy(
            registry,
            CopyRequest(
                ResourceLocation("source", "source"), ResourceLocation("target", "new/output")
            ),
        )
    (stage,) = tmp_path.glob("new/.ridge-transfer-*")
    assert "injected staging cleanup failure" in format_error(caught.value)
    assert stage.name in format_error(caught.value)
    assert not (tmp_path / "new/output").exists()
    assert stage.exists()


def test_sigterm_during_staging_allows_acknowledged_cleanup(tmp_path: Path) -> None:
    stream = LocalResource("target", tmp_path).open_transfer_destination("new/output", "file")
    assert isinstance(stream, ProcessTransferDestination)
    stream.write(b"incoming")
    stream.process.send_signal(signal.SIGTERM)
    with pytest.raises(TransferError, match="cancelled"):
        stream.cancel()
    stream.abort()
    assert not (tmp_path / "new").exists()


def test_unbuffered_destination_retries_short_writes(tmp_path: Path) -> None:
    stream = LocalResource("target", tmp_path).open_transfer_destination("output", "file")
    assert isinstance(stream, ProcessTransferDestination)
    original = stream._stdin
    assert original is not None

    class ShortWriter:
        def write(self, content: bytes | memoryview) -> int:
            return original.write(content[:3])

    stream._stdin = cast(BinaryIO, ShortWriter())
    content = b"a complete payload" * 100
    stream.write(content)
    assert stream.finish() == (len(content), 1)
    stream.commit()
    assert (tmp_path / "output").read_bytes() == content
    assert not list(tmp_path.glob(".ridge-transfer-*"))


def test_missing_phase_record_retains_stage_despite_stop_report(tmp_path: Path) -> None:
    stream = FaultyDestination(tmp_path, "metadata").open_transfer_destination("new/output", "file")
    stream.write(b"incoming")
    with pytest.raises(TransferError, match="phase recording"):
        stream.finish()
    with pytest.raises(TransferError, match="cleanup refused") as caught:
        stream.abort()
    (stage,) = tmp_path.glob("new/.ridge-transfer-*")
    assert stage.name in format_error(caught.value)
    assert (stage / "payload").read_bytes() == b"incoming"
    assert json.loads((stage / ".ridge-transfer.json").read_text())["phase"] == "receiving"
    assert not (tmp_path / "new/output").exists()


def test_crash_before_readiness_reports_target_without_claiming_cleanup(tmp_path: Path) -> None:
    with pytest.raises(ResourceUnavailableError) as caught:
        FaultyDestination(tmp_path, "startup_crash").open_transfer_destination("new/output", "file")
    assert "new/output" in format_error(caught.value)
    (stage,) = tmp_path.glob("new/.ridge-transfer-*")
    assert not (stage / "payload").exists()
    assert not (tmp_path / "new/output").exists()


def test_staging_startup_failure_creates_no_stage(tmp_path: Path) -> None:
    (tmp_path / "directory").mkdir()
    with pytest.raises(Exception, match="types differ") as caught:
        LocalResource("target", tmp_path).open_transfer_destination("directory", "file")
    assert "staging startup" in format_error(caught.value)
    assert not list(tmp_path.glob(".ridge-transfer-*"))


@pytest.fixture
def anyio_backend() -> Literal["asyncio"]:
    return "asyncio"


def structured(response: CallToolResult) -> dict[str, object]:
    assert not response.is_error
    assert response.structured_content is not None
    return cast(dict[str, object], response.structured_content)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["file", "tree"])
@pytest.mark.parametrize("frontend", ["cli", "mcp", "background", "cancel"])
async def test_canonical_copy_interruption_cleanup(
    tmp_path: Path, kind: str, frontend: str
) -> None:
    source_root = tmp_path / "source-root"
    target_root = tmp_path / "target-root"
    source_root.mkdir()
    target_root.mkdir()
    source = source_root / "input"
    if kind == "tree":
        source.mkdir()
        source = source / "data"
    source.write_bytes(b"x" * 524288)
    config = tmp_path / "ridge.yaml"
    config.write_text(
        "resources:\n  source: {provider: local, root: source-root}\n"
        "  target: {provider: local, root: target-root}\n"
    )
    injection = tmp_path / "injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text("""
import os
import time
from pathlib import Path
from ridge.backends._transfer import ProcessTransferSource
from ridge.errors import TransferError
original_read = ProcessTransferSource.read
def read(self, size):
    reads = getattr(self, "_test_reads", 0)
    if self.resource_name == "source" and reads == 4:
        if os.environ["RIDGE_TEST_FRONTEND"] == "cancel":
            Path(os.environ["RIDGE_TEST_MARKER"]).touch()
            time.sleep(30)
        raise TransferError("injected midstream source failure")
    self._test_reads = reads + 1
    return original_read(self, size)
ProcessTransferSource.read = read
""")
    marker = tmp_path / "waiting"
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            (str(injection), str(Path(__file__).resolve().parents[1] / "src"))
        ),
        "RIDGE_TEST_FRONTEND": frontend,
        "RIDGE_TEST_MARKER": str(marker),
    }
    if frontend == "cli":
        with anyio.fail_after(15):
            result = await anyio.run_process(
                [
                    sys.executable,
                    "-c",
                    "from ridge.cli import main; main()",
                    "--config",
                    str(config),
                    "copy",
                    "source:input",
                    "target:new/output",
                ],
                env=environment,
                check=False,
            )
        assert result.returncode == 2
        assert b"injected midstream source failure" in result.stderr
    else:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-c", "from ridge.mcp import main; main()", "--config", str(config)],
            env=environment,
        )
        with anyio.fail_after(25):
            async with Client(parameters) as client:
                response = await client.call_tool(
                    "copy",
                    {
                        "source": "source:input",
                        "destination": "target:new/output",
                        "background": frontend in ("background", "cancel"),
                    },
                )
                if frontend == "mcp":
                    assert response.is_error
                    assert response.content[0].type == "text"
                    assert "injected midstream source failure" in response.content[0].text
                else:
                    job = cast(dict[str, object], structured(response)["job"])
                    job_id = str(job["id"])
                    if frontend == "cancel":
                        while not marker.exists():
                            await anyio.sleep(0.02)
                        assert list(target_root.glob("new/.ridge-transfer-*"))
                        await client.call_tool("cancel_job", {"job_id": job_id})
                    while True:
                        inspected = structured(
                            await client.call_tool("inspect_job", {"job_id": job_id})
                        )
                        if inspected["status"] in ("succeeded", "failed", "cancelled", "lost"):
                            break
                        await anyio.sleep(0.02)
                    assert inspected["status"] == (
                        "cancelled" if frontend == "cancel" else "failed"
                    )
                    if frontend == "background":
                        assert "injected midstream source failure" in str(inspected["error"])
    assert source.read_bytes() == b"x" * 524288
    assert not (target_root / "new").exists()
    assert not list(target_root.glob("**/.ridge-transfer-*"))
