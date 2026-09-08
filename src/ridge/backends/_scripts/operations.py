"""Standalone standard-library helper, sent as source to remote Python."""

import base64
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Never

# At runtime deletion is prepended to this script for the remote interpreter.
if TYPE_CHECKING:
    from ridge.backends._scripts.deletion import DeletePathError, delete_path  # noqa: TC004


def reply(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")


def fail(kind: str, message: str) -> Never:
    reply({"ok": False, "error": kind, "message": message})
    raise SystemExit(0)


def rooted(root_text: str, requested_text: str = ".") -> tuple[Path, Path]:
    requested = Path(requested_text)
    if requested.is_absolute():
        fail("invalid_path", "resource paths must be relative: " + requested_text)
    try:
        root = Path(root_text).resolve(strict=True)
    except FileNotFoundError:
        fail("path_not_found", "resource root does not exist: " + root_text)
    except OSError as error:
        fail("invalid_path", "cannot resolve resource root " + root_text + ": " + str(error))
    if not root.is_dir():
        fail("path_type", "resource root is not a directory: " + root_text)
    try:
        target = (root / requested).resolve(strict=False)
        target.relative_to(root)
    except (OSError, ValueError):
        fail("invalid_path", "path escapes resource root: " + requested_text)
    return root, target


def kind(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    if path.is_file():
        return "file"
    if path.is_dir():
        return "directory"
    return "other"


def main() -> None:
    request_line = sys.stdin.buffer.readline()
    try:
        request = json.loads(request_line)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail("protocol", "invalid Ridge helper request: " + str(error))

    operation = sys.argv[2]
    root_text = sys.argv[1]
    path_text = request.get("path", ".")
    if operation == "delete":
        try:
            outcome = delete_path(Path(root_text), path_text, recursive=request["recursive"])
        except DeletePathError as error:
            fail(error.kind, str(error) + "; deletion may be partial; no rollback")
        except (OSError, RuntimeError) as error:
            fail("execution", str(error) + "; deletion may be partial; no rollback")
        reply({"ok": True, "outcome": outcome})
        return
    root, target = rooted(root_text, path_text)

    if operation == "exec":
        argv = request["argv"]
        if not argv:
            fail("execution", "argv must contain at least one argument")
        if not target.exists():
            fail("path_not_found", "working directory does not exist: " + path_text)
        if not target.is_dir():
            fail("path_type", "working directory is not a directory: " + path_text)
        process_env = os.environ.copy()
        process_env.update(request.get("env") or {})
        timeout = request.get("timeout_seconds")
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                cwd=target,
                env=process_env,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            fail("execution_timeout", "command exceeded its " + str(timeout) + "-second timeout")
        except OSError as error:
            fail("execution", "cannot start command " + repr(argv[0]) + ": " + str(error))
        reply(
            {
                "ok": True,
                "argv": argv,
                "exit_code": completed.returncode,
                "stdout": base64.b64encode(completed.stdout).decode("ascii"),
                "stderr": base64.b64encode(completed.stderr).decode("ascii"),
                "duration_seconds": time.monotonic() - started,
            }
        )
    elif operation == "list":
        if not target.exists():
            fail("path_not_found", "path does not exist: " + path_text)
        if not target.is_dir():
            fail("path_type", "path is not a directory: " + path_text)
        entries: list[dict[str, Any]] = []
        for child in sorted(target.iterdir(), key=lambda item: item.name):
            metadata = child.lstat()
            relative = child.relative_to(root).as_posix()
            entries.append({"path": relative, "kind": kind(child), "size": metadata.st_size})
        reply({"ok": True, "entries": entries})
    elif operation == "read":
        if not target.exists():
            fail("path_not_found", "path does not exist: " + path_text)
        if not target.is_file():
            fail("path_type", "path is not a file: " + path_text)
        maximum = request.get("max_bytes")
        size = target.stat().st_size
        if maximum is not None and size > maximum:
            fail(
                "output_limit",
                "file is "
                + str(size)
                + " bytes, exceeding the "
                + str(maximum)
                + "-byte limit: "
                + path_text,
            )
        with target.open("rb") as stream:
            content = stream.read() if maximum is None else stream.read(maximum + 1)
        if maximum is not None and len(content) > maximum:
            fail("output_limit", "file exceeds the " + str(maximum) + "-byte limit: " + path_text)
        reply({"ok": True, "content": base64.b64encode(content).decode("ascii")})
    elif operation == "write":
        content = sys.stdin.buffer.read()
        if target == root:
            fail("path_type", "path is a directory: " + path_text)
        unresolved = root / Path(path_text)
        try:
            unresolved.parent.mkdir(parents=True, exist_ok=True)
            parent = unresolved.parent.resolve(strict=True)
            parent.relative_to(root)
            target = parent / unresolved.name
        except (OSError, RuntimeError, ValueError) as error:
            fail(
                "invalid_path",
                "cannot create destination parents for " + path_text + ": " + str(error),
            )
        if target.exists() and target.is_dir() and not target.is_symlink():
            fail("path_type", "path is a directory: " + path_text)
        if target.exists() and not (target.is_file() or target.is_symlink()):
            fail("path_type", "path is not a regular file or symbolic link: " + path_text)
        descriptor = None
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".ridge-write-", dir=target.parent)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(content)
            os.replace(temporary, target)
        except OSError as error:
            fail("execution", "cannot write " + path_text + ": " + str(error))
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        reply({"ok": True})
    elif operation == "stat":
        if not target.exists() and not target.is_symlink():
            fail("path_not_found", "path does not exist: " + path_text)
        metadata = target.lstat()
        relative = target.relative_to(root)
        display = "." if str(relative) == "." else relative.as_posix()
        reply(
            {
                "ok": True,
                "stat": {
                    "path": display,
                    "kind": kind(target),
                    "size": metadata.st_size,
                    "modified_ns": metadata.st_mtime_ns,
                },
            }
        )
    elif operation == "probe":
        reply(
            {
                "ok": True,
                "properties": {
                    "remote.os": platform.system().lower(),
                    "remote.arch": platform.machine().lower(),
                    "remote.hostname": platform.node(),
                    "helper.python_version": platform.python_version(),
                },
            }
        )
    else:
        fail("protocol", "unknown Ridge helper operation: " + operation)


if __name__ == "__main__":
    main()
