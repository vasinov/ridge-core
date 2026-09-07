import json
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ridge.authorization import AuthorizationPolicy
from ridge.cli import app
from ridge.config import LoadedConfiguration
from ridge.model import (
    ObjectEntry,
    ObjectPage,
    ObjectStat,
    ResourceProperty,
)
from ridge.registry import ResourceRegistry
from ridge.resource import ResourceCapabilities

runner = CliRunner()


class _StorageFixture:
    name = "storage"
    provider_name = "fixture"

    def __init__(self) -> None:
        self.content = b"initial"
        self.capabilities = ResourceCapabilities(storage=self)

    def inspect_properties(self) -> dict[str, ResourceProperty]:
        return {}

    def list_objects(
        self, prefix: str = "", *, cursor: str | None = None, limit: int = 1000
    ) -> ObjectPage:
        del prefix, cursor, limit
        return ObjectPage((ObjectEntry("key", len(self.content), "etag", None),), None)

    def read_object(self, key: str, *, max_bytes: int | None = None) -> bytes:
        del key, max_bytes
        return self.content

    def write_object(self, key: str, content: bytes) -> None:
        del key
        self.content = content

    def stat_object(self, key: str) -> ObjectStat:
        return ObjectStat(key, len(self.content), "etag", None)


def _config(tmp_path: Path) -> Path:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {local: {provider: local, root: .}}")
    return config


def test_exec_options_are_parsed_after_resource_and_program_flags_are_preserved(
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "--config",
            str(_config(tmp_path)),
            "exec",
            "local",
            "--cwd",
            ".",
            "--timeout",
            "1.5",
            "--",
            sys.executable,
            "-c",
            "import sys; print(sys.argv[1])",
            "--program-option",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "--program-option\n"


def test_exec_returns_child_exit_code(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--config",
            str(_config(tmp_path)),
            "exec",
            "local",
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ],
    )

    assert result.exit_code == 7


def test_expected_error_has_stable_exit_code_and_message(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["--config", str(_config(tmp_path)), "read", "local", "/etc/passwd"],
    )

    assert result.exit_code == 2
    assert "ridge: resource paths must be relative" in result.stderr


def test_configured_permissions_deny_cli_write_before_side_effect(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text(
        "resources: {local: {provider: local, root: .}}\npermissions:\n  local: [data.read]\n"
    )

    listing = runner.invoke(app, ["--config", str(config), "resources"])
    denied = runner.invoke(
        app,
        ["--config", str(config), "write", "local", "output.txt", "--text", "no"],
    )

    assert listing.exit_code == 0
    assert "SUPPORTED\tALLOWED" in listing.stdout
    assert listing.stdout.rstrip().endswith("data.read")
    assert denied.exit_code == 2
    assert "authorization denied for data.write" in denied.stderr
    assert not (tmp_path / "output.txt").exists()


def test_list_accepts_optional_positional_path(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "file.txt").write_text("ridge")

    result = runner.invoke(
        app,
        ["--config", str(_config(tmp_path)), "list", "local", "nested"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "addressing": "filesystem",
        "entries": [{"kind": "file", "size": 5, "path": "nested/file.txt"}],
        "next_cursor": None,
    }


def test_data_operations_exist_only_at_root(tmp_path: Path) -> None:
    config = _config(tmp_path)

    root_level = runner.invoke(
        app,
        ["--config", str(config), "write", "local", "result.txt", "--text", "ridge"],
    )
    namespaced = runner.invoke(
        app,
        ["--config", str(config), "fs", "write", "local", "other.txt", "--text", "ridge"],
    )

    assert root_level.exit_code == 0
    assert (tmp_path / "result.txt").read_text() == "ridge"
    assert namespaced.exit_code == 2
    assert "No such command 'fs'" in namespaced.output
    assert not (tmp_path / "other.txt").exists()


def test_copy_command_uses_resource_locations(tmp_path: Path) -> None:
    (tmp_path / "input.txt").write_text("ridge")

    result = runner.invoke(
        app,
        [
            "--config",
            str(_config(tmp_path)),
            "copy",
            "local:input.txt",
            "local:nested/output.txt",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "copied 5 bytes in 1 entry\n"
    assert (tmp_path / "nested/output.txt").read_text() == "ridge"


def test_background_cli_write_returns_job_and_lifecycle_commands_reconnect(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    submitted = runner.invoke(
        app,
        [
            "--config",
            str(config),
            "write",
            "local",
            "background.txt",
            "--text",
            "ridge",
            "--background",
        ],
    )

    assert submitted.exit_code == 0
    job_id = submitted.stdout.strip().removeprefix("submitted ")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        inspected = runner.invoke(app, ["--config", str(config), "jobs", "inspect", job_id])
        if '"status": "succeeded"' in inspected.stdout:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("CLI job did not complete")
    listed = runner.invoke(app, ["--config", str(config), "jobs", "list"])

    assert inspected.exit_code == 0
    assert job_id in listed.stdout
    assert (tmp_path / "background.txt").read_text() == "ridge"


def test_shared_data_commands_use_object_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _StorageFixture()
    registry = ResourceRegistry([storage])

    def load_fixture(_: Path) -> LoadedConfiguration:
        return LoadedConfiguration(registry, AuthorizationPolicy.unrestricted())

    monkeypatch.setattr("ridge.application.load_configuration", load_fixture)

    written = runner.invoke(app, ["write", "storage", "key", "--text", "updated"])
    read = runner.invoke(app, ["read", "storage", "key"])
    listed = runner.invoke(app, ["list", "storage"])
    stated = runner.invoke(app, ["stat", "storage", "key"])

    assert written.exit_code == 0
    assert read.stdout == "updated"
    assert '"key": "key"' in listed.stdout
    assert '"size": 7' in stated.stdout


def test_removed_storage_namespace_is_rejected(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["--config", str(_config(tmp_path)), "storage", "list", "local"],
    )

    assert result.exit_code == 2
    assert "No such command 'storage'" in result.stderr


@pytest.mark.parametrize("source_allowed", [False, True])
@pytest.mark.parametrize("background", [False, True])
def test_cli_copy_checks_both_data_grants(
    tmp_path: Path,
    source_allowed: bool,
    background: bool,
) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text(
        "resources: {source: {provider: local}, dest: {provider: local}}\n"
        + (
            "permissions: {source: [data.read]}"
            if source_allowed
            else "permissions: {dest: [data.write]}"
        )
    )
    args = ["--config", str(config), "copy", "source:missing", "dest:nested/output"]
    if background:
        args.append("--background")
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert f"authorization denied for data.{'write' if source_allowed else 'read'}" in result.stderr
    assert not (tmp_path / "nested").exists()
    assert not (tmp_path / ".ridge").exists()
