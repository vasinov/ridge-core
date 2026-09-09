import json
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from typer.testing import CliRunner

from ridge.backends.local import LocalResource
from ridge.cli import app
from ridge.config import load_configuration
from ridge.errors import ConfigurationError, format_error
from ridge.provider import ProviderContext, ResourceProviderRegistry

runner = CliRunner()


def test_validation_summary_matches_loader_without_runtime_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text(
        """resources:
  local:
    provider: local
    root: .
    properties: {private: do-not-echo-this}
  alias:
    provider: local
    root: .
    lock_key: local
  docker:
    provider: docker
    container: unavailable
    root: /workspace
    python: missing-python
  ssh:
    provider: ssh
    host: unavailable.invalid
    root: /workspace
    python: missing-python
  s3:
    provider: s3
    bucket: unavailable-bucket
permissions:
  local: [data.read, data.stat]
  s3: [data.delete]
delegation:
  local: [data.read, data.write]
state: {directory: state-not-created}
"""
    )
    forbidden = Mock(side_effect=AssertionError("validation must not operate on resources"))
    for target in (
        "ridge.cli._service",
        "subprocess.Popen",
        "boto3.client",
        "ridge.backends.local.LocalResource.inspect_properties",
        "ridge.backends.docker.DockerResource.inspect_properties",
        "ridge.backends.ssh.SshResource.inspect_properties",
        "ridge.backends.s3.S3Resource.inspect_properties",
    ):
        monkeypatch.setattr(target, forbidden)
    before = config.read_bytes()
    loaded = load_configuration(config)
    result = runner.invoke(app, ["--config", str(config), "config", "validate", "--json"])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["valid"] is True
    assert report["permission_mode"] == "exact"
    assert report["config"] == str(config.resolve())
    assert report["state_directory"] == str((tmp_path / "state-not-created").resolve())
    assert [item["name"] for item in report["resources"]] == list(loaded.registry.names())
    for item in report["resources"]:
        name = item["name"]
        assert item["allowed_operations"] == [
            op.value
            for op in loaded.registry.get(name).capabilities.operations
            if loaded.authorization.allows(name, op)
        ]
        assert item["lock_key"] == loaded.lock_keys[name]
        assert item["delegable_operations"] == [
            op.value
            for op in loaded.registry.get(name).capabilities.operations
            if loaded.authorization.allows(name, op) and loaded.delegation.allows(name, op)
        ]
        assert set(item) == {
            "name",
            "provider",
            "lock_key",
            "allowed_operations",
            "delegable_operations",
        }
    assert "do-not-echo-this" not in result.stdout
    assert list(tmp_path.iterdir()) == [config]
    assert config.read_bytes() == before
    forbidden.assert_not_called()


@pytest.mark.parametrize("policy", [None, {}, {"local": []}, {"local": ["data.delete"]}])
def test_policy_modes_and_text_output(tmp_path: Path, policy: object) -> None:
    config = tmp_path / "ridge.yaml"
    document: dict[str, object] = {"resources": {"local": {"provider": "local", "root": "."}}}
    if policy is not None:
        document["permissions"] = policy
    config.write_text(yaml.safe_dump(document))
    result = runner.invoke(app, ["--config", str(config), "config", "validate"])
    assert result.exit_code == 0
    assert result.stderr == ""
    mode = "unrestricted" if policy is None else "exact"
    assert f"Permissions: {mode}" in result.stdout
    item = json.loads(result.stdout.splitlines()[-1])
    assert ("data.delete" in item["allowed_operations"]) == (
        policy in (None, {"local": ["data.delete"]})
    )
    assert not (tmp_path / ".ridge").exists()


@pytest.mark.parametrize(
    "content",
    [
        b"resources: [",
        b"\xff",
        b"resources: {}\nunknown: true",
        b"resources: {data: {provider: nope}}",
        b"resources: {data: {provider: local, root: nonexistent}}",
        b"resources: {data: {provider: local, typo: true}}",
        b"resources: {data: {provider: local}}\npermissions: {missing: [data.read]}",
        b"resources: {data: {provider: local}}\npermissions: {data: [data.read, data.read]}",
        b"resources: {data: {provider: local}}\npermissions: {data: [bad.op]}",
        b"resources: {data: {provider: s3, bucket: example}}\npermissions: {data: [compute.exec]}",
        b"resources: {data: {provider: ssh, host: host, root: /work, python: python3, identity_file: missing}}",
        b"resources: {}\nstate: {directory: 42}",
        b"resources: {data: {provider: local, lock_key: invalid/key}}",
        None,
    ],
)
def test_invalid_result_is_first_loader_error(tmp_path: Path, content: bytes | None) -> None:
    config = tmp_path / "ridge.yaml"
    if content is not None:
        config.write_bytes(content)
    with pytest.raises(ConfigurationError) as error:
        load_configuration(config)
    for extra in ([], ["--json"]):
        result = runner.invoke(app, ["--config", str(config), "config", "validate", *extra])
        assert result.exit_code == 2
        if extra:
            assert json.loads(result.stdout) == {"valid": False, "error": format_error(error.value)}
            assert result.stderr == ""
        else:
            assert result.stdout == ""
            assert format_error(error.value) in result.stderr
    assert not (tmp_path / ".ridge").exists()


def test_config_selection_and_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "inventory"
    config_dir.mkdir()
    config = config_dir / "ridge.yaml"
    config.write_text("resources: {local: {provider: local, root: .}}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RIDGE_CONFIG", str(config))
    result = runner.invoke(app, ["config", "validate", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["state_directory"] == str(config_dir / ".ridge")
    override = tmp_path / "ridge.yaml"
    override.write_text("resources: {}\npermissions: {}")
    result = runner.invoke(app, ["--config", "ridge.yaml", "config", "validate", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["resources"] == []
    monkeypatch.delenv("RIDGE_CONFIG")
    result = runner.invoke(app, ["config", "validate", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["config"] == str(override)


def test_custom_provider_constructor_runs_but_not_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ResourceProviderRegistry()
    calls: list[str] = []

    def create(name: str, config: Mapping[str, object], context: ProviderContext) -> LocalResource:
        calls.append(name)
        resource = LocalResource(name, context.config_dir)
        resource.provider_name = "custom"
        resource.inspect_properties = Mock(side_effect=AssertionError("no inspection"))
        return resource

    registry.register("custom", create)
    monkeypatch.setattr("ridge.config.default_provider_registry", lambda: registry)
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {data: {provider: custom}}")
    result = runner.invoke(app, ["--config", str(config), "config", "validate", "--json"])
    assert result.exit_code == 0, result.output
    assert calls == ["data"]
    assert json.loads(result.stdout)["resources"][0]["provider"] == "custom"


def test_portable_example_validation() -> None:
    config = Path(__file__).resolve().parents[1] / "ridge.example.yaml"
    result = runner.invoke(app, ["--config", str(config), "config", "validate", "--json"])
    assert result.exit_code == 0, result.output
    resources = json.loads(result.stdout)["resources"]
    assert resources[0]["lock_key"] == resources[1]["lock_key"]
