import io
import os
import stat
import sys
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from ridge._job_process import in_job_worker
from ridge.backends._source import TRANSFER_HELPER_SOURCE
from ridge.backends.docker import DockerResource
from ridge.backends.local import LocalResource
from ridge.backends.ssh import SshResource
from ridge.conformance import check_file_transfer_capability
from ridge.errors import InvalidPathError, PathTypeError, SourceChangedError
from ridge.model import (
    CopyRequest,
    FileStat,
    ResourceLocation,
    ResourceProperty,
    TransferPayloadKind,
)
from ridge.registry import ResourceRegistry
from ridge.resource import ResourceCapabilities, TransferDestination, TransferSource
from ridge.transfer import copy


def _request(source: str, destination: str) -> CopyRequest:
    return CopyRequest(ResourceLocation.parse(source), ResourceLocation.parse(destination))


@pytest.mark.parametrize("background", [False, True])
def test_transfer_process_session_follows_job_ownership(tmp_path: Path, background: bool) -> None:
    import subprocess

    (tmp_path / "source").write_bytes(b"owned transfer")
    registry = ResourceRegistry([LocalResource("local", tmp_path)])
    token = in_job_worker.set(background)
    try:
        with patch("subprocess.Popen", wraps=subprocess.Popen) as launch:
            result = copy(registry, _request("local:source", "local:output"))
        assert result.bytes_copied == 14
        assert len(launch.call_args_list) >= 3  # export, staging, publication control
        assert all(
            call.kwargs["start_new_session"] is not background for call in launch.call_args_list
        )
        assert (tmp_path / "output").read_bytes() == b"owned transfer"
    finally:
        in_job_worker.reset(token)


def test_reusable_file_transfer_conformance(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_root.mkdir()
    destination_root.mkdir()
    source = LocalResource("source", source_root)
    destination = LocalResource("destination", destination_root)
    source.write("input.bin", b"\x00ridge\xff")
    assert source.capabilities.transfer is not None
    assert destination.capabilities.transfer is not None

    check_file_transfer_capability(
        source.capabilities.transfer,
        destination.capabilities.transfer,
        source_path="input.bin",
        destination_path="output.bin",
        expected_content=b"\x00ridge\xff",
        read_destination=destination.read,
    )


class _LocalTransferDockerResource(DockerResource):
    def _transfer_command(self, operation: str) -> tuple[str, ...]:
        return (sys.executable, "-c", TRANSFER_HELPER_SOURCE, self.root, operation)

    def stat(self, path: str) -> FileStat:
        return LocalResource("fixture-stat", Path(self.root)).stat(path)


class _LocalTransferSshResource(SshResource):
    def _transfer_command(self, operation: str) -> tuple[str, ...]:
        return (sys.executable, "-c", TRANSFER_HELPER_SOURCE, self.root, operation)

    def stat(self, path: str) -> FileStat:
        return LocalResource("fixture-stat", Path(self.root)).stat(path)


def _backend_resource(
    backend: str, name: str, root: Path
) -> LocalResource | DockerResource | SshResource:
    if backend == "local":
        return LocalResource(name, root)
    if backend == "docker":
        return _LocalTransferDockerResource(
            name,
            container="fixture",
            root=str(root),
            python_executable=sys.executable,
        )
    return _LocalTransferSshResource(
        name,
        host="fixture",
        root=str(root),
        python_executable=sys.executable,
    )


def test_resource_location_requires_explicit_resource_and_path() -> None:
    assert ResourceLocation.parse("build:jobs/input.txt") == ResourceLocation(
        "build", "jobs/input.txt"
    )
    with pytest.raises(ValueError, match="RESOURCE:PATH"):
        ResourceLocation.parse("missing-path")


def test_copy_streams_binary_file_to_exact_new_location_and_creates_ancestors(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_root.mkdir()
    destination_root.mkdir()
    content = bytes(range(256)) * 1024
    (source_root / "input.bin").write_bytes(content)
    registry = ResourceRegistry(
        [
            LocalResource("source", source_root),
            LocalResource("destination", destination_root),
        ]
    )

    result = copy(registry, _request("source:input.bin", "destination:new/nested/output.bin"))

    assert (destination_root / "new/nested/output.bin").read_bytes() == content
    assert result.bytes_copied == len(content)
    assert result.entries_copied == 1


@pytest.mark.parametrize("source_backend", ["local", "docker", "ssh"])
@pytest.mark.parametrize("destination_backend", ["local", "docker", "ssh"])
def test_copy_conforms_across_every_backend_pair(
    tmp_path: Path, source_backend: str, destination_backend: str
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_root.mkdir()
    destination_root.mkdir()
    content = b"\x00ridge\xff" * 16384
    (source_root / "input.bin").write_bytes(content)
    registry = ResourceRegistry(
        [
            _backend_resource(source_backend, "source", source_root),
            _backend_resource(destination_backend, "destination", destination_root),
        ]
    )

    result = copy(registry, _request("source:input.bin", "destination:output.bin"))

    assert result.bytes_copied == len(content)
    assert (destination_root / "output.bin").read_bytes() == content


def test_copy_preserves_tree_shape_modes_and_safe_relative_symlinks(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    project = source_root / "project"
    (project / "bin").mkdir(parents=True)
    (project / "empty").mkdir()
    script = project / "bin/run"
    script.write_bytes(b"#!/bin/sh\necho ridge\n")
    script.chmod(0o751)
    (project / "current").symlink_to("bin/run")
    destination_root.mkdir()
    registry = ResourceRegistry(
        [LocalResource("source", source_root), LocalResource("destination", destination_root)]
    )

    result = copy(registry, _request("source:project", "destination:artifacts/project"))

    copied = destination_root / "artifacts/project"
    assert (copied / "empty").is_dir()
    assert (copied / "bin/run").read_bytes() == script.read_bytes()
    assert stat.S_IMODE((copied / "bin/run").stat().st_mode) == 0o751
    assert (copied / "current").is_symlink()
    assert os.readlink(copied / "current") == "bin/run"
    assert result.entries_copied == 5


def test_copy_replaces_existing_file(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_root.mkdir()
    destination_root.mkdir()
    (source_root / "input.txt").write_text("new")
    target = destination_root / "output.txt"
    target.write_text("existing")
    registry = ResourceRegistry(
        [LocalResource("source", source_root), LocalResource("destination", destination_root)]
    )

    copy(registry, _request("source:input.txt", "destination:output.txt"))

    assert target.read_text() == "new"


def test_copy_replaces_complete_existing_tree_without_merging(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    (source_root / "project").mkdir(parents=True)
    (source_root / "project/current.txt").write_text("current")
    (destination_root / "project").mkdir(parents=True)
    (destination_root / "project/stale.txt").write_text("stale")
    registry = ResourceRegistry(
        [LocalResource("source", source_root), LocalResource("destination", destination_root)]
    )

    copy(registry, _request("source:project", "destination:project"))

    assert (destination_root / "project/current.txt").read_text() == "current"
    assert not (destination_root / "project/stale.txt").exists()


@pytest.mark.parametrize("source_name,destination_name", [("file", "tree"), ("tree", "file")])
def test_copy_rejects_file_tree_type_mismatch(
    tmp_path: Path, source_name: str, destination_name: str
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_root.mkdir()
    destination_root.mkdir()
    (source_root / "file").write_text("file")
    (source_root / "tree").mkdir()
    (destination_root / "file").write_text("unchanged")
    (destination_root / "tree").mkdir()
    (destination_root / "tree/unchanged").write_text("unchanged")
    registry = ResourceRegistry(
        [LocalResource("source", source_root), LocalResource("destination", destination_root)]
    )

    with pytest.raises(PathTypeError, match="types differ"):
        copy(
            registry,
            _request(f"source:{source_name}", f"destination:{destination_name}"),
        )

    if destination_name == "file":
        assert (destination_root / "file").read_text() == "unchanged"
    else:
        assert (destination_root / "tree/unchanged").read_text() == "unchanged"


@pytest.mark.parametrize("link_target", ["/etc/passwd", "missing", "../../outside"])
def test_copy_rejects_unsafe_tree_symlink_without_publishing(
    tmp_path: Path, link_target: str
) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    project = source_root / "project"
    project.mkdir(parents=True)
    destination_root.mkdir()
    (project / "link").symlink_to(link_target)
    registry = ResourceRegistry(
        [LocalResource("source", source_root), LocalResource("destination", destination_root)]
    )

    with pytest.raises(InvalidPathError):
        copy(registry, _request("source:project", "destination:nested/project"))

    assert not (destination_root / "nested").exists()


def test_copy_rejects_special_files(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    project = source_root / "project"
    project.mkdir(parents=True)
    destination_root.mkdir()
    os.mkfifo(project / "events")
    registry = ResourceRegistry(
        [LocalResource("source", source_root), LocalResource("destination", destination_root)]
    )

    with pytest.raises(PathTypeError, match="special files"):
        copy(registry, _request("source:project", "destination:project"))

    assert not (destination_root / "project").exists()


def test_copy_rejects_hard_linked_files(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    project = source_root / "project"
    project.mkdir(parents=True)
    destination_root.mkdir()
    original = project / "original"
    original.write_text("ridge")
    os.link(original, project / "alias")
    registry = ResourceRegistry(
        [LocalResource("source", source_root), LocalResource("destination", destination_root)]
    )

    with pytest.raises(PathTypeError, match="hard-linked"):
        copy(registry, _request("source:project", "destination:project"))

    assert not (destination_root / "project").exists()


def test_abort_removes_read_only_staged_tree_and_all_created_ancestors(tmp_path: Path) -> None:
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
        root = tarfile.TarInfo("payload")
        root.type = tarfile.DIRTYPE
        root.mode = 0o500
        archive.addfile(root)
        child = tarfile.TarInfo("payload/file.txt")
        child.mode = 0o400
        child.size = 5
        archive.addfile(child, io.BytesIO(b"ridge"))
    destination = LocalResource("destination", tmp_path)

    staged = destination.open_transfer_destination("one/two/project", "tree")
    staged.write(archive_bytes.getvalue())
    assert staged.finish() == (5, 2)
    staged.abort()

    assert not (tmp_path / "one").exists()


class _MutatingDestination:
    def __init__(self, delegate: LocalResource, source_path: Path) -> None:
        self.name = delegate.name
        self.provider_name = delegate.provider_name
        self._delegate = delegate
        self._source_path = source_path
        self.capabilities = ResourceCapabilities(filesystem=delegate, transfer=self)

    def inspect_properties(self) -> dict[str, ResourceProperty]:
        return dict(self._delegate.inspect_properties())

    def open_transfer_source(self, path: str) -> TransferSource:
        return self._delegate.open_transfer_source(path)

    def open_transfer_destination(
        self, path: str, kind: TransferPayloadKind
    ) -> TransferDestination:
        destination = self._delegate.open_transfer_destination(path, kind)
        source_path = self._source_path

        class MutatingTransferDestination:
            def __init__(self) -> None:
                self._mutated = False

            def write(self, content: bytes) -> None:
                if not self._mutated:
                    self._mutated = True
                    with source_path.open("r+b") as stream:
                        stream.seek(0)
                        stream.write(b"changed")
                destination.write(content)

            def finish(self) -> tuple[int, int]:
                return destination.finish()

            def commit(self) -> None:
                destination.commit()

            def abort(self) -> None:
                destination.abort()

            def cancel(self) -> None:
                destination.cancel()

        return MutatingTransferDestination()


def test_source_change_discards_staged_destination_and_created_ancestors(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_root.mkdir()
    destination_root.mkdir()
    source_path = source_root / "large.bin"
    source_path.write_bytes(b"x" * (4 * 1024 * 1024))
    source = LocalResource("source", source_root)
    destination = _MutatingDestination(LocalResource("destination", destination_root), source_path)
    registry = ResourceRegistry([source, destination])

    with pytest.raises(SourceChangedError):
        copy(registry, _request("source:large.bin", "destination:new/output.bin"))

    assert not (destination_root / "new").exists()
    assert list(destination_root.glob("**/.ridge-transfer-*")) == []


class _InterruptingSourceStream:
    kind: TransferPayloadKind = "file"

    def __init__(self) -> None:
        self.reads = 0
        self.cancelled = False

    def read(self, size: int) -> bytes:
        del size
        self.reads += 1
        if self.reads == 1:
            return b"archive"
        raise KeyboardInterrupt

    def finish(self) -> None:
        raise AssertionError("interrupted source must not finish")

    def cancel(self) -> None:
        self.cancelled = True


class _RecordingDestinationStream:
    def __init__(self) -> None:
        self.cancelled = False
        self.aborted = False

    def write(self, content: bytes) -> None:
        assert content == b"archive"

    def finish(self) -> tuple[int, int]:
        raise AssertionError("interrupted destination must not finish")

    def commit(self) -> None:
        raise AssertionError("interrupted destination must not commit")

    def abort(self) -> None:
        self.aborted = True

    def cancel(self) -> None:
        self.cancelled = True


class _InterruptingSourceResource(LocalResource):
    def __init__(self, name: str, root: Path, stream: _InterruptingSourceStream) -> None:
        super().__init__(name, root)
        self.stream = stream

    def open_transfer_source(self, path: str) -> TransferSource:
        del path
        return self.stream


class _RecordingDestinationResource(LocalResource):
    def __init__(self, name: str, root: Path, stream: _RecordingDestinationStream) -> None:
        super().__init__(name, root)
        self.stream = stream

    def open_transfer_destination(
        self, path: str, kind: TransferPayloadKind
    ) -> TransferDestination:
        del path, kind
        return self.stream


def test_cancellation_closes_both_endpoints_and_aborts_destination(tmp_path: Path) -> None:
    source_stream = _InterruptingSourceStream()
    destination_stream = _RecordingDestinationStream()
    registry = ResourceRegistry(
        [
            _InterruptingSourceResource("source", tmp_path, source_stream),
            _RecordingDestinationResource("destination", tmp_path, destination_stream),
        ]
    )

    with pytest.raises(KeyboardInterrupt):
        copy(registry, _request("source:input", "destination:output"))

    assert source_stream.cancelled
    assert destination_stream.cancelled
    assert destination_stream.aborted


def test_copy_rejects_same_location_before_side_effects(tmp_path: Path) -> None:
    registry = ResourceRegistry([LocalResource("local", tmp_path)])

    with pytest.raises(InvalidPathError, match="must be different"):
        copy(registry, _request("local:nested/../file", "local:file"))
