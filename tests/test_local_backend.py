import subprocess
import sys
from pathlib import Path

import pytest

from ridge.backends.local import LocalResource
from ridge.errors import (
    ExecutionError,
    ExecutionTimeoutError,
    InvalidPathError,
    OutputLimitExceededError,
)


def test_filesystem_round_trip_and_sorted_listing(tmp_path: Path) -> None:
    resource = LocalResource("files", tmp_path)
    resource.write("nested/b.txt", b"bravo")
    resource.write("nested/a.txt", b"alpha")

    assert resource.read("nested/a.txt") == b"alpha"
    assert [entry.path for entry in resource.list("nested")] == [
        "nested/a.txt",
        "nested/b.txt",
    ]
    assert resource.stat("nested/a.txt").kind == "file"


def test_write_creates_parents_and_replaces_file_by_default(tmp_path: Path) -> None:
    resource = LocalResource("files", tmp_path)
    resource.write("nested/result.txt", b"first")
    resource.write("nested/result.txt", b"second")

    assert resource.read("nested/result.txt") == b"second"


def test_write_replaces_an_in_root_symbolic_link(tmp_path: Path) -> None:
    resource = LocalResource("files", tmp_path)
    (tmp_path / "target").mkdir()
    (tmp_path / "link").symlink_to("target", target_is_directory=True)

    resource.write("link", b"replacement")

    assert not (tmp_path / "link").is_symlink()
    assert resource.read("link") == b"replacement"
    assert (tmp_path / "target").is_dir()


@pytest.mark.parametrize("path", ["../outside.txt", "/tmp/outside.txt"])
def test_rejects_paths_outside_root(tmp_path: Path, path: str) -> None:
    resource = LocalResource("files", tmp_path)

    with pytest.raises(InvalidPathError):
        resource.read(path)


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    resource = LocalResource("files", root)

    with pytest.raises(InvalidPathError):
        resource.read("escape/secret.txt")


def test_write_rejects_traversal_before_creating_parents(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    resource = LocalResource("files", root)

    with pytest.raises(InvalidPathError):
        resource.write("../outside/nested.txt", b"no")

    assert not (tmp_path / "outside").exists()


def test_read_bound_is_enforced(tmp_path: Path) -> None:
    (tmp_path / "large.bin").write_bytes(b"1234")
    resource = LocalResource("files", tmp_path)

    with pytest.raises(OutputLimitExceededError):
        resource.read("large.bin", max_bytes=3)


def test_exec_uses_argv_without_a_shell_and_honors_cwd(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    resource = LocalResource("local", tmp_path)

    result = resource.exec(
        [sys.executable, "-c", "import os,sys; print(os.getcwd()); print(sys.argv[1])", "a;b"],
        cwd="nested",
    )

    assert result.exit_code == 0
    assert result.stdout.decode().splitlines() == [str(nested), "a;b"]


def test_exec_has_no_default_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    observed_timeout: float | None = 1

    def run_fixture(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal observed_timeout
        timeout = kwargs.get("timeout")
        assert timeout is None or isinstance(timeout, (int, float))
        observed_timeout = timeout
        return subprocess.CompletedProcess(["fixture"], 0, b"", b"")

    monkeypatch.setattr("ridge.backends.local.subprocess.run", run_fixture)

    LocalResource("local", tmp_path).exec(["fixture"])

    assert observed_timeout is None


def test_exec_timeout_is_reported(tmp_path: Path) -> None:
    resource = LocalResource("local", tmp_path)

    with pytest.raises(ExecutionTimeoutError):
        resource.exec([sys.executable, "-c", "import time; time.sleep(1)"], timeout_seconds=0.01)


def test_exec_start_failure_is_a_ridge_error(tmp_path: Path) -> None:
    resource = LocalResource("local", tmp_path)

    with pytest.raises(ExecutionError, match="cannot start command"):
        resource.exec(["definitely-not-a-ridge-test-executable"])
