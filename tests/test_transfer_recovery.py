"""Faults run inside real disposable filesystem helpers, not mock cleanup methods."""

# pyright: reportPrivateUsage=false
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ridge.backends._source import TRANSFER_HELPER_SOURCE
from ridge.backends.local import LocalResource
from ridge.errors import ResourceUnavailableError, TransferError, format_error
from ridge.model import CopyRequest, ResourceLocation
from ridge.registry import ResourceRegistry
from ridge.transfer import copy


def fault_source(fault: str) -> str:
    return (
        f"FAULT = {fault!r}\n"
        + r"""
import os
import shutil
import sys
from pathlib import Path

original_rename = os.rename
original_replace = os.replace
original_remove = shutil.rmtree
if sys.argv[2] == "commit":
    def rename(source, target):
        name = Path(source).name
        if name == "payload" and FAULT in ("publish", "rollback", "record_rollback"):
            raise OSError("injected publication failure")
        if name == "replaced" and FAULT == "rollback":
            raise OSError("injected rollback failure")
        original_rename(source, target)
        if Path(target).name == "replaced" and FAULT == "crash":
            os._exit(3)
        if name == "payload" and FAULT == "crash_published":
            os._exit(3)
    os.rename = rename
    def replace(source, target):
        if FAULT == "record_rollback" and '"phase":"rolled_back"' in Path(source).read_text():
            raise OSError("injected phase recording failure")
        return original_replace(source, target)
    os.replace = replace
    def remove(path, *args, **kwargs):
        if FAULT in ("cleanup", "partial_cleanup"):
            if FAULT == "partial_cleanup":
                (Path(path) / ".ridge-transfer.json").unlink()
            raise OSError("injected cleanup failure")
        return original_remove(path, *args, **kwargs)
    shutil.rmtree = remove
    if FAULT == "ack":
        sys.stdout = open(os.devnull, "w")
"""
        + TRANSFER_HELPER_SOURCE
    )


class FaultyLocalResource(LocalResource):
    def __init__(self, name: str, root: Path, fault: str) -> None:
        super().__init__(name, root)
        self.fault = fault

    def _transfer_command(self, operation: str) -> tuple[str, ...]:
        return (sys.executable, "-c", fault_source(self.fault), str(self.root), operation)


@pytest.mark.parametrize("kind", ["file", "tree", "symlink"])
@pytest.mark.parametrize(
    "fault",
    ["publish", "rollback", "cleanup", "partial_cleanup", "crash", "crash_published", "ack"],
)
def test_publication_failure_preserves_data_and_reports_recovery(
    tmp_path: Path, kind: str, fault: str
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    if kind == "tree":
        source.mkdir()
        (source / "new").write_bytes(b"new data")
        target.mkdir()
        (target / "old").write_bytes(b"original data")
    else:
        source.write_bytes(b"new data")
        if kind == "symlink":
            (tmp_path / "original").write_bytes(b"original data")
            target.symlink_to("original")
        else:
            target.write_bytes(b"original data")
    registry = ResourceRegistry(
        [LocalResource("source", tmp_path), FaultyLocalResource("target", tmp_path, fault)]
    )

    with pytest.raises((TransferError, ResourceUnavailableError)) as caught:
        copy(
            registry,
            CopyRequest(ResourceLocation("source", "source"), ResourceLocation("target", "target")),
        )

    message = format_error(caught.value)
    assert "resource 'target'" in message
    assert (source / "new" if kind == "tree" else source).read_bytes() == b"new data"
    stages = list(tmp_path.glob(".ridge-transfer-*"))
    if fault == "publish":
        assert (target / "old" if kind == "tree" else target).read_bytes() == b"original data"
        assert not stages
        assert "rolled_back" in message
        return
    if fault == "ack":
        assert (target / "new" if kind == "tree" else target).read_bytes() == b"new data"
        assert not stages
        assert "unconfirmed" in message
        return

    assert len(stages) == 1
    stage = stages[0]
    backup = stage / "replaced"
    preserved = backup / "old" if kind == "tree" else backup
    if kind == "symlink":
        preserved = tmp_path / "original"
    assert preserved.read_bytes() == b"original data"
    if kind == "symlink":
        assert backup.is_symlink()
        assert backup.readlink() == Path("original")
        assert (tmp_path / "original").read_bytes() == b"original data"
    assert stage.name in message
    if fault in ("cleanup", "partial_cleanup", "crash_published"):
        assert (target / "new" if kind == "tree" else target).read_bytes() == b"new data"
        assert ("unconfirmed" if fault == "crash_published" else "published") in message
    else:
        assert not target.exists()
        assert "rollback_failed" in message if fault == "rollback" else "unconfirmed" in message
    if fault != "partial_cleanup":
        phase = json.loads((stage / ".ridge-transfer.json").read_text())["phase"]
        assert (
            phase
            == {
                "rollback": "rollback_failed",
                "cleanup": "published",
                "crash": "publishing",
                "crash_published": "publishing",
            }[fault]
        )

    # A fresh helper must independently refuse cleanup/republication, even if
    # the original coordinator is gone. Neither operation may consume the backup.
    for operation in ("abort", "commit"):
        response = subprocess.run(
            [sys.executable, "-c", TRANSFER_HELPER_SOURCE, str(tmp_path), operation],
            input=json.dumps({"token": stage.name}).encode() + b"\n",
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert response.returncode == 2
        assert preserved.read_bytes() == b"original data"
        assert backup.is_symlink() if kind == "symlink" else backup.exists()


def test_unknown_publication_is_not_retried_or_aborted(tmp_path: Path) -> None:
    target = FaultyLocalResource("target", tmp_path, "ack")
    stream = target.open_transfer_destination("output", "file")
    stream.write(b"published")
    stream.finish()
    with pytest.raises(ResourceUnavailableError, match="acknowledgement"):
        stream.commit()
    with pytest.raises(TransferError, match="cannot be retried"):
        stream.commit()
    with pytest.raises(TransferError, match="unconfirmed publication"):
        stream.abort()
    assert (tmp_path / "output").read_bytes() == b"published"


def test_abort_preserves_backup_even_if_metadata_says_staged(tmp_path: Path) -> None:
    stream = LocalResource("target", tmp_path).open_transfer_destination("output", "file")
    stream.write(b"incoming")
    stream.finish()
    (stage,) = tmp_path.glob(".ridge-transfer-*")
    (stage / "replaced").write_bytes(b"recoverable")
    with pytest.raises(TransferError, match="cleanup refused"):
        stream.abort()
    assert (stage / "replaced").read_bytes() == b"recoverable"


def test_recovery_metadata_failure_does_not_mask_publication_error(tmp_path: Path) -> None:
    (tmp_path / "source").write_bytes(b"new")
    (tmp_path / "target").write_bytes(b"old")
    registry = ResourceRegistry(
        [
            LocalResource("source", tmp_path),
            FaultyLocalResource("target", tmp_path, "record_rollback"),
        ]
    )
    with pytest.raises(TransferError) as caught:
        copy(
            registry,
            CopyRequest(ResourceLocation("source", "source"), ResourceLocation("target", "target")),
        )
    message = format_error(caught.value)
    assert "injected publication failure" in message
    assert "injected phase recording failure" in message
    assert "cleanup refused" in message
    assert (tmp_path / "target").read_bytes() == b"old"
    (stage,) = tmp_path.glob(".ridge-transfer-*")
    assert (stage / "payload").read_bytes() == b"new"
    assert json.loads((stage / ".ridge-transfer.json").read_text())["phase"] == "publishing"
