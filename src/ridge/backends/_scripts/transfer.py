"""Standalone standard-library transfer helper with persisted recovery phases."""

import json
import os
import posixpath
import shutil
import signal
import stat
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from types import FrameType
from typing import Any, Never, cast

CHUNK_SIZE = 64 * 1024
METADATA_NAME = ".ridge-transfer.json"
TOKEN_PREFIX = ".ridge-transfer-"


def fail(kind: str, message: object) -> Never:
    sys.stderr.write(
        json.dumps({"error": kind, "message": str(message)}, separators=(",", ":")) + "\n"
    )
    sys.stderr.flush()
    os._exit(2)


def reply(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def resource_root(root_text: str) -> Path:
    try:
        root = Path(root_text).resolve(strict=True)
    except FileNotFoundError:
        fail("path_not_found", "resource root does not exist: " + root_text)
    except OSError as error:
        fail("invalid_path", "cannot resolve resource root " + root_text + ": " + str(error))
    if not root.is_dir():
        fail("path_type", "resource root is not a directory: " + root_text)
    return root


def leaf_path(root: Path, path_text: str) -> Path:
    requested = Path(path_text)
    if requested.is_absolute():
        fail("invalid_path", "resource paths must be relative: " + path_text)
    unresolved = root / requested
    try:
        if unresolved == root:
            target = root
        else:
            parent = unresolved.parent.resolve(strict=False)
            parent.relative_to(root)
            target = parent / unresolved.name
            target.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError):
        fail("invalid_path", "path escapes resource root: " + path_text)
    return target


def fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def snapshot_entry(path: Path, archive_name: str, tree_root: Path) -> dict[str, Any]:
    metadata = path.lstat()
    value: dict[str, Any] = {
        "path": path,
        "name": archive_name,
        "fingerprint": fingerprint(metadata),
        "mode": stat.S_IMODE(metadata.st_mode),
    }
    if stat.S_ISREG(metadata.st_mode):
        if metadata.st_nlink != 1:
            fail("path_type", "hard-linked files cannot be copied: " + str(path))
        value["kind"] = "file"
        value["size"] = metadata.st_size
    elif stat.S_ISDIR(metadata.st_mode):
        value["kind"] = "directory"
        value["children"] = tuple(sorted(child.name for child in os.scandir(path)))
    elif stat.S_ISLNK(metadata.st_mode):
        link = os.readlink(path)
        if not link or os.path.isabs(link):
            fail("invalid_path", "symbolic link must have a relative target: " + str(path))
        try:
            resolved = (path.parent / link).resolve(strict=True)
            resolved.relative_to(tree_root)
        except (OSError, RuntimeError, ValueError):
            fail("invalid_path", "symbolic link is broken or escapes copied tree: " + str(path))
        value["kind"] = "symlink"
        value["link"] = link
    else:
        fail("path_type", "special files cannot be copied: " + str(path))
    return value


def snapshot(path: Path) -> list[dict[str, Any]]:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        fail("path_type", "a top-level symbolic link cannot be copied: " + str(path))
    if stat.S_ISREG(metadata.st_mode):
        return [snapshot_entry(path, "payload", path.parent)]
    if not stat.S_ISDIR(metadata.st_mode):
        fail("path_type", "copy source must be a regular file or directory: " + str(path))

    entries: list[dict[str, Any]] = []

    def visit(current: Path, archive_name: str) -> None:
        entry = snapshot_entry(current, archive_name, path)
        entries.append(entry)
        if entry["kind"] != "directory":
            return
        for child_name in entry["children"]:
            visit(current / child_name, archive_name + "/" + child_name)

    visit(path, "payload")
    return entries


def verify_entry(entry: dict[str, Any]) -> None:
    path = entry["path"]
    try:
        metadata = path.lstat()
    except OSError:
        fail("source_changed", "copy source changed during transfer: " + str(path))
    if fingerprint(metadata) != entry["fingerprint"]:
        fail("source_changed", "copy source changed during transfer: " + str(path))
    if entry["kind"] == "directory":
        try:
            children = tuple(sorted(child.name for child in os.scandir(path)))
        except OSError:
            fail("source_changed", "copy source changed during transfer: " + str(path))
        if children != entry["children"]:
            fail("source_changed", "copy source changed during transfer: " + str(path))
    elif entry["kind"] == "symlink" and os.readlink(path) != entry["link"]:
        fail("source_changed", "copy source changed during transfer: " + str(path))


def export_file(root: Path, request: dict[str, Any]) -> None:
    path_text = request["path"]
    source = leaf_path(root, path_text)
    if not source.exists():
        fail("path_not_found", "path does not exist: " + path_text)
    entry = snapshot_entry(source, "payload", source.parent)
    if entry["kind"] != "file":
        fail("path_type", "copy source is not a regular file: " + path_text)
    verify_entry(entry)
    try:
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        stream = os.fdopen(descriptor, "rb")
    except OSError:
        fail("source_changed", "copy source changed during transfer: " + str(source))
    with stream:
        if fingerprint(os.fstat(stream.fileno())) != entry["fingerprint"]:
            fail("source_changed", "copy source changed during transfer: " + str(source))
        while chunk := stream.read(CHUNK_SIZE):
            sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        if fingerprint(os.fstat(stream.fileno())) != entry["fingerprint"]:
            fail("source_changed", "copy source changed during transfer: " + str(source))
    verify_entry(entry)


def export_tree(root: Path, request: dict[str, Any]) -> None:
    path_text = request["path"]
    source = leaf_path(root, path_text)
    if not source.exists() and not source.is_symlink():
        fail("path_not_found", "path does not exist: " + path_text)
    entries = snapshot(source)
    if entries[0]["kind"] != "directory":
        fail("path_type", "copy source is not a directory: " + path_text)
    archive = tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.PAX_FORMAT)  # noqa: SIM115 - close only after source verification
    for entry in entries:
        verify_entry(entry)
        info = tarfile.TarInfo(entry["name"])
        info.mode = entry["mode"] & 0o777
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = 0
        if entry["kind"] == "directory":
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        elif entry["kind"] == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = entry["link"]
            archive.addfile(info)
        else:
            info.size = entry["size"]
            try:
                descriptor = os.open(entry["path"], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                stream = os.fdopen(descriptor, "rb")
            except OSError:
                fail("source_changed", "copy source changed during transfer: " + str(entry["path"]))
            with stream:
                if fingerprint(os.fstat(stream.fileno())) != entry["fingerprint"]:
                    fail(
                        "source_changed",
                        "copy source changed during transfer: " + str(entry["path"]),
                    )
                archive.addfile(info, stream)
                if fingerprint(os.fstat(stream.fileno())) != entry["fingerprint"]:
                    fail(
                        "source_changed",
                        "copy source changed during transfer: " + str(entry["path"]),
                    )
    for entry in entries:
        verify_entry(entry)
    archive.close()


def safe_member_path(stage: Path, member_name: str) -> Path:
    name = PurePosixPath(member_name)
    if name.is_absolute() or not name.parts or name.parts[0] != "payload":
        raise ValueError("invalid path in transfer archive: " + member_name)
    if any(part in ("", ".", "..") for part in name.parts):
        raise ValueError("invalid path in transfer archive: " + member_name)
    if name.as_posix() != member_name:
        raise ValueError("non-canonical path in transfer archive: " + member_name)
    return stage.joinpath(*name.parts)


def remove_tree(path: Path) -> None:
    def make_removable(directory: Path) -> None:
        directory.chmod(0o700)
        for child in os.scandir(directory):
            if child.is_dir(follow_symlinks=False):
                make_removable(Path(child.path))

    make_removable(path)
    shutil.rmtree(path)


def clean_created_parents(root: Path, created: list[str]) -> None:
    for relative in created:
        try:
            candidate = root.joinpath(*PurePosixPath(relative).parts)
            candidate.rmdir()
        except OSError:
            pass


def prepare_destination(root: Path, path_text: str, payload_kind: str) -> tuple[Path, list[str]]:
    target = leaf_path(root, path_text)
    if target.exists() or target.is_symlink():
        if (
            payload_kind == "file"
            and (target.is_file() or target.is_symlink())
            or payload_kind == "tree"
            and target.is_dir()
            and not target.is_symlink()
        ):
            pass
        else:
            fail("path_type", "copy source and destination types differ: " + path_text)
    missing: list[str] = []
    candidate = target.parent
    while candidate != root and not candidate.exists():
        try:
            missing.append(candidate.relative_to(root).as_posix())
        except ValueError:
            fail("invalid_path", "path escapes resource root: " + path_text)
        candidate = candidate.parent
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target = leaf_path(root, path_text)
        target.parent.resolve(strict=True).relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        clean_created_parents(root, missing)
        fail(
            "invalid_path", "cannot create destination parents for " + path_text + ": " + str(error)
        )
    return target, missing


def cancel_staging(signum: int, frame: FrameType | None) -> Never:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise InterruptedError("destination staging cancelled")


def begin_stage(root: Path, path_text: str, payload_kind: str) -> tuple[Path, dict[str, Any]]:
    target, created = prepare_destination(root, path_text, payload_kind)
    stage = None
    try:
        stage = Path(tempfile.mkdtemp(prefix=TOKEN_PREFIX, dir=target.parent))
        metadata: dict[str, Any] = {
            "target": path_text,
            "kind": payload_kind,
            "phase": "receiving",
            "created_parents": created,
        }
        record_phase(stage, metadata, "receiving")
        signal.signal(signal.SIGTERM, cancel_staging)
        reply({"ok": True, "ready": True, "token": stage.relative_to(root).as_posix()})
        return stage, metadata
    except BaseException as error:
        if stage is not None:
            try:
                remove_tree(stage)
            except OSError as cleanup_error:
                fail(
                    "transfer",
                    str(error)
                    + "; startup cleanup failed: "
                    + str(cleanup_error)
                    + "; staging path: "
                    + str(stage),
                )
        clean_created_parents(root, created)
        raise


def staging_failed(
    root: Path, stage: Path, metadata: dict[str, Any], error: BaseException
) -> Never:
    detail = str(error) or type(error).__name__
    try:
        record_phase(stage, metadata, "staging_failed")
    except OSError as metadata_error:
        detail += "; cannot record staging phase: " + str(metadata_error)
    # No payload writes follow this acknowledgement, even when recording fails.
    reply({"ok": False, "stopped": True, "token": stage.relative_to(root).as_posix()})
    fail("transfer", detail + "; " + recovery_detail(stage, metadata))


def stage_file(root: Path, request: dict[str, Any]) -> None:
    stage, metadata = begin_stage(root, request["path"], "file")
    payload = stage / "payload"
    bytes_copied = 0
    try:
        descriptor = os.open(payload, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            while chunk := sys.stdin.buffer.read(CHUNK_SIZE):
                output.write(chunk)
                bytes_copied += len(chunk)
        metadata.update(bytes_copied=bytes_copied, entries_copied=1)
        record_phase(stage, metadata, "staged")
    except BaseException as error:  # noqa: BLE001 - report stopped staging or terminal helper failure
        staging_failed(root, stage, metadata, error)
    reply(
        {
            "ok": True,
            "stopped": True,
            "token": stage.relative_to(root).as_posix(),
            "bytes_copied": bytes_copied,
            "entries_copied": 1,
        }
    )


def stage_tree(root: Path, request: dict[str, Any]) -> None:
    stage, metadata = begin_stage(root, request["path"], "tree")
    seen: set[str] = set()
    directory_modes: list[tuple[Path, int]] = []
    symlinks: list[Path] = []
    bytes_copied = 0
    entries_copied = 0
    try:
        archive = tarfile.open(fileobj=sys.stdin.buffer, mode="r|")  # noqa: SIM115 - finish archive before draining payload input
        for member in archive:
            destination = safe_member_path(stage, member.name)
            if member.name in seen:
                raise ValueError("duplicate path in transfer archive: " + member.name)
            seen.add(member.name)
            if member.isdir():
                destination.mkdir(mode=0o700)
                directory_modes.append((destination, member.mode & 0o777))
            elif member.isreg():
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("missing file content in transfer archive: " + member.name)
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            raise ValueError("truncated file in transfer archive: " + member.name)
                        output.write(chunk)
                        remaining -= len(chunk)
                        bytes_copied += len(chunk)
                destination.chmod(member.mode & 0o777)
            elif member.issym():
                if not member.linkname or posixpath.isabs(member.linkname):
                    raise ValueError("unsafe symbolic link in transfer archive: " + member.name)
                normalized = posixpath.normpath(
                    posixpath.join(posixpath.dirname(member.name), member.linkname)
                )
                if normalized != "payload" and not normalized.startswith("payload/"):
                    raise ValueError("escaping symbolic link in transfer archive: " + member.name)
                destination.symlink_to(member.linkname)
                symlinks.append(destination)
            else:
                raise ValueError("unsupported member in transfer archive: " + member.name)
            entries_copied += 1
        archive.close()
        while sys.stdin.buffer.read(CHUNK_SIZE):
            pass
        payload = stage / "payload"
        if "payload" not in seen or (not payload.is_file() and not payload.is_dir()):
            raise ValueError("transfer archive has no file or directory payload")
        for link in symlinks:
            resolved = link.resolve(strict=True)
            resolved.relative_to(payload if payload.is_dir() else payload.parent)
        for directory, mode in reversed(directory_modes):
            directory.chmod(mode)
        metadata.update(bytes_copied=bytes_copied, entries_copied=entries_copied)
        record_phase(stage, metadata, "staged")
    except BaseException as error:  # noqa: BLE001 - report stopped staging or terminal helper failure
        staging_failed(root, stage, metadata, error)
    reply(
        {
            "ok": True,
            "stopped": True,
            "token": stage.relative_to(root).as_posix(),
            "bytes_copied": bytes_copied,
            "entries_copied": entries_copied,
        }
    )


def stage_from_token(root: Path, token: str) -> Path:
    relative = PurePosixPath(token)
    if relative.is_absolute() or not relative.name.startswith(TOKEN_PREFIX):
        fail("transfer", "invalid transfer token")
    try:
        stage = root.joinpath(*relative.parts).resolve(strict=True)
        stage.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        fail("transfer", "invalid transfer token")
    if not stage.is_dir() or not (stage / METADATA_NAME).is_file():
        fail("transfer", "invalid transfer token")
    return stage


def load_metadata(stage: Path) -> dict[str, Any]:
    try:
        value = json.loads((stage / METADATA_NAME).read_text())
    except (OSError, json.JSONDecodeError) as error:
        fail("transfer", "invalid staged transfer: " + str(error))
    if not isinstance(value, dict):
        fail("transfer", "invalid staged transfer metadata")
    return cast(dict[str, Any], value)


def record_phase(stage: Path, metadata: dict[str, Any], phase: str) -> None:
    metadata["phase"] = phase
    temporary = stage / (METADATA_NAME + ".tmp")
    temporary.write_text(json.dumps(metadata, separators=(",", ":")))
    temporary.replace(stage / METADATA_NAME)


def recovery_detail(stage: Path, metadata: dict[str, Any]) -> str:
    return (
        "publication state "
        + str(metadata.get("phase", "unknown"))
        + "; inspect destination "
        + str(metadata["target"])
        + "; staging path: "
        + str(stage)
        + "; previous destination, if retained: "
        + str(stage / "replaced")
    )


def commit(root: Path, request: dict[str, Any]) -> None:
    stage = stage_from_token(root, request["token"])
    metadata = load_metadata(stage)
    if metadata.get("phase") != "staged":
        fail("transfer", "publication cannot be retried; " + recovery_detail(stage, metadata))
    path_text = metadata["target"]
    target = leaf_path(root, path_text)
    payload = stage / "payload"
    payload_kind = metadata["kind"]
    exists = target.exists() or target.is_symlink()
    if exists:
        if (
            payload_kind == "file"
            and (target.is_file() or target.is_symlink())
            or payload_kind == "tree"
            and target.is_dir()
            and not target.is_symlink()
        ):
            pass
        else:
            fail("path_type", "copy source and destination types differ: " + path_text)
    replaced = stage / "replaced"
    try:
        if payload_kind == "file" and not payload.is_file():
            fail("transfer", "staged transfer payload is invalid")
        if payload_kind == "tree" and not payload.is_dir():
            fail("transfer", "staged transfer payload is invalid")
        # Record intent before moving user data. Abort must preserve staging if
        # the helper dies or loses its acknowledgement during publication.
        record_phase(stage, metadata, "publishing")
        if exists:
            os.rename(target, replaced)
        try:
            os.rename(payload, target)
        except OSError as publication_error:
            detail = "cannot publish destination " + path_text + ": " + str(publication_error)
            phase = "rolled_back"
            try:
                if exists:
                    os.rename(replaced, target)
            except OSError as rollback_error:
                phase = "rollback_failed"
                detail += "; rollback also failed: " + str(rollback_error)
            try:
                record_phase(stage, metadata, phase)
            except OSError as metadata_error:
                detail += "; cannot record recovery phase: " + str(metadata_error)
            fail("transfer", detail + "; " + recovery_detail(stage, metadata))
        record_phase(stage, metadata, "published")
        remove_tree(stage)
    except OSError as error:
        fail(
            "transfer",
            "destination commit failed: " + str(error) + "; " + recovery_detail(stage, metadata),
        )
    reply({"ok": True})


def abort(root: Path, request: dict[str, Any]) -> None:
    stage = stage_from_token(root, request["token"])
    metadata = load_metadata(stage)
    if metadata.get("phase") not in ("staged", "staging_failed", "rolled_back"):
        fail("transfer", "automatic cleanup refused; " + recovery_detail(stage, metadata))
    # A backup is user data even if phase metadata is incomplete or inconsistent.
    if (stage / "replaced").exists() or (stage / "replaced").is_symlink():
        fail("transfer", "automatic cleanup refused; " + recovery_detail(stage, metadata))
    created = metadata.get("created_parents", [])
    try:
        remove_tree(stage)
    finally:
        clean_created_parents(root, created)
    reply({"ok": True})


def main() -> None:
    try:
        request = json.loads(sys.stdin.buffer.readline())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail("transfer", "invalid Ridge transfer request: " + str(error))

    root = resource_root(sys.argv[1])
    operation = sys.argv[2]
    try:
        if operation == "export-file":
            export_file(root, request)
        elif operation == "export-tree":
            export_tree(root, request)
        elif operation == "stage-file":
            stage_file(root, request)
        elif operation == "stage-tree":
            stage_tree(root, request)
        elif operation == "commit":
            commit(root, request)
        elif operation == "abort":
            abort(root, request)
        else:
            fail("transfer", "unknown transfer operation: " + operation)
    except KeyError as error:
        fail("transfer", "missing transfer field: " + str(error))
    except KeyboardInterrupt:
        raise
    except BaseException as error:  # noqa: BLE001 - report stopped staging or terminal helper failure
        fail("transfer", str(error))


if __name__ == "__main__":
    main()
