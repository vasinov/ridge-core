from pathlib import Path
from unittest.mock import Mock

import pytest

from ridge.backends.docker import DockerResource
from ridge.backends.s3 import S3Resource
from ridge.backends.ssh import SshResource
from ridge.config import load_configuration, load_registry
from ridge.errors import ConfigurationError
from ridge.model import Operation


def test_identity_mismatch_precedes_provider_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {data: {provider: local, root: .}}")
    discover = Mock(side_effect=AssertionError("provider code must not run"))
    monkeypatch.setattr("ridge.config.default_provider_registry", discover)
    with pytest.raises(ConfigurationError, match="resource 'data' changed after submission"):
        load_configuration(config, expected_resource_identities={"data": "original"})
    discover.assert_not_called()


def test_configuration_constructs_only_semantically_checked_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "ridge.yaml"
    original = b"resources: {original: {provider: local, root: .}}"
    config.write_bytes(original)
    identities = load_configuration(config).resource_identities
    read_bytes = Path.read_bytes
    reads = 0

    def swap_after_read(path: Path) -> bytes:
        nonlocal reads
        content = read_bytes(path)
        if path == config:
            reads += 1
            path.write_bytes(b"resources: {replacement: {provider: local, root: .}}")
        return content

    monkeypatch.setattr(Path, "read_bytes", swap_after_read)
    loaded = load_configuration(config, expected_resource_identities=identities)
    assert loaded.registry.names() == ("original",)
    assert loaded.resource_identities == identities
    assert reads == 1


def test_resource_identity_ignores_mapping_order_and_yaml_presentation(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text(
        "resources: {data: {provider: local, root: ., properties: {a: 1, b: true}}}\n"
        "permissions: {data: [data.read, data.write]}\n"
    )
    before = load_configuration(config)
    config.write_text(
        "# edited with different YAML presentation\n"
        "permissions: {data: [data.write, data.read]}\n"
        "resources:\n  data:\n    properties: {b: true, a: 1}\n    root: '.'\n    provider: local\n"
    )
    assert load_configuration(config).resource_identities == before.resource_identities
    assert not (tmp_path / ".ridge").exists()


@pytest.mark.parametrize("value", ["'1'", "true", "1.0"])
def test_resource_identity_preserves_scalar_types(tmp_path: Path, value: str) -> None:
    config = tmp_path / "ridge.yaml"
    original = "resources: {data: {provider: local, properties: {value: 1}}}"
    config.write_text(original)
    before = load_configuration(config).resource_identities
    config.write_text(original.replace("value: 1", f"value: {value}"))
    assert load_configuration(config).resource_identities != before


def test_recursive_provider_configuration_fails_cleanly(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {data: &recursive {provider: local, properties: *recursive}}")
    with pytest.raises(ConfigurationError, match="recursive configuration"):
        load_configuration(config)


def test_default_and_explicit_state_resolve_identically(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    (tmp_path / "state").mkdir()
    (tmp_path / ".ridge").symlink_to(tmp_path / "state", target_is_directory=True)
    config.write_text("resources: {}")
    implicit = load_configuration(config)
    config.write_text("resources: {}\nstate: {directory: .ridge}")
    explicit = load_configuration(config, expected_state_directory=implicit.state_directory)
    assert explicit.state_directory == implicit.state_directory == tmp_path / "state"


def test_portable_example_includes_shared_coordination_state(tmp_path: Path) -> None:
    example = Path(__file__).resolve().parents[1] / "ridge.example.yaml"
    config = tmp_path / "ridge.yaml"
    config.write_text(example.read_text())

    loaded = load_configuration(config)

    assert loaded.state_directory == (tmp_path.parent / ".ridge-example-state").resolve()
    assert loaded.lock_keys["local"] == loaded.lock_keys["project-files"]
    assert loaded.lock_keys["local"] == "project-workspace"


def test_loads_resources_and_resolves_roots_relative_to_config(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "ridge.yaml"
    config.write_text(
        """resources:
  machine:
    provider: local
  data:
    provider: local
    root: data
    properties:
      purpose: fixtures
"""
    )

    registry = load_registry(config)

    assert registry.names() == ("data", "machine")
    inspection = registry.inspect("data")
    assert inspection.supported_operations == (
        Operation.COMPUTE_EXEC,
        Operation.DATA_LIST,
        Operation.DATA_READ,
        Operation.DATA_WRITE,
        Operation.DATA_STAT,
        Operation.DATA_DELETE,
    )
    assert inspection.allowed_operations == inspection.supported_operations
    assert inspection.properties["purpose"].source == "configured"
    assert inspection.properties["location"].source == "detected"


def test_loads_exact_permissions_and_treats_empty_map_as_default_deny(
    tmp_path: Path,
) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text(
        """resources:
  data:
    provider: local
    root: .
permissions:
  data:
    - data.read
    - data.stat
"""
    )

    loaded = load_configuration(config)

    assert not loaded.authorization.unrestricted_mode
    assert loaded.authorization.allows("data", Operation.DATA_READ)
    assert not loaded.authorization.allows("data", Operation.DATA_WRITE)

    config.write_text("resources: {data: {provider: local, root: .}}\npermissions: {}\n")
    denied = load_configuration(config)
    assert not denied.authorization.unrestricted_mode
    assert not denied.authorization.allows("data", Operation.DATA_READ)


def test_state_directory_is_relative_to_configuration(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text(
        "resources: {data: {provider: local, root: .}}\nstate: {directory: state/jobs}\n"
    )

    loaded = load_configuration(config)

    assert loaded.path == config.resolve()
    assert loaded.resource_identities.keys() == {"data"}
    assert loaded.state_directory == (tmp_path / "state" / "jobs").resolve()


@pytest.mark.parametrize(
    "permissions,match",
    [
        ("missing: [data.read]", "unknown resource"),
        ("data: data.read", "must be a list"),
        ("data: [filesystem.delete]", "unknown operation"),
        ("data: [data.read, data.read]", "duplicate permission"),
    ],
)
def test_rejects_invalid_permissions(tmp_path: Path, permissions: str, match: str) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text(
        f"resources: {{data: {{provider: local, root: .}}}}\npermissions:\n  {permissions}\n"
    )

    with pytest.raises(ConfigurationError, match=match):
        load_configuration(config)


def test_removed_filesystem_provider_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {data: {provider: filesystem, root: .}}")

    with pytest.raises(ConfigurationError, match="unsupported provider"):
        load_registry(config)


@pytest.mark.parametrize("grant", ["filesystem.read", "storage.write", "transfer.read"])
def test_removed_permission_names_are_rejected(tmp_path: Path, grant: str) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text(f"resources: {{data: {{provider: local}}}}\npermissions: {{data: [{grant}]}}")
    with pytest.raises(ConfigurationError, match="unknown operation"):
        load_configuration(config)


def test_old_type_key_and_unsupported_compute_grant_are_rejected(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {data: {type: local}}")
    with pytest.raises(ConfigurationError, match="requires a string provider"):
        load_registry(config)
    config.write_text(
        "resources: {data: {provider: s3, bucket: test}}\npermissions: {data: [compute.exec]}"
    )
    with pytest.raises(ConfigurationError, match="does not support granted operations"):
        load_configuration(config)


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {local: {provider: local, typo: true}}")

    with pytest.raises(ConfigurationError, match="unknown keys"):
        load_registry(config)


def test_missing_root_is_reported_as_configuration_error(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {data: {provider: local, root: missing}}")

    with pytest.raises(ConfigurationError, match="resource root does not exist"):
        load_registry(config)


def test_loads_docker_resource_without_contacting_daemon(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text(
        """resources:
  build:
    provider: docker
    container: ridge-build
    root: /workspace
    python: python3
    executable: /opt/docker
"""
    )

    resource = load_registry(config).get("build")

    assert isinstance(resource, DockerResource)
    assert resource.name == "build"
    assert resource.container == "ridge-build"
    assert resource.root == "/workspace"


@pytest.mark.parametrize("missing", ["container", "root", "python"])
def test_docker_resource_requires_contract_fields(tmp_path: Path, missing: str) -> None:
    values = {
        "container": "ridge-build",
        "root": "/workspace",
        "python": "python3",
    }
    del values[missing]
    lines = ["resources:", "  build:", "    provider: docker"]
    lines.extend(f"    {key}: {value}" for key, value in values.items())
    config = tmp_path / "ridge.yaml"
    config.write_text("\n".join(lines))

    with pytest.raises(ConfigurationError, match="requires"):
        load_registry(config)


def test_loads_ssh_resource_and_resolves_local_files_from_config(tmp_path: Path) -> None:
    (tmp_path / "identity").write_text("fixture")
    (tmp_path / "known_hosts").write_text("fixture")
    config = tmp_path / "ridge.yaml"
    config.write_text(
        """resources:
  remote:
    provider: ssh
    host: build.example.test
    user: ridge
    port: 2222
    root: /workspace
    python: python3
    identity_file: identity
    known_hosts_file: known_hosts
"""
    )

    resource = load_registry(config).get("remote")

    assert isinstance(resource, SshResource)
    assert resource.host == "build.example.test"
    assert resource.identity_file == (tmp_path / "identity")
    assert resource.known_hosts_file == (tmp_path / "known_hosts")


def test_s3_resources_can_select_different_regions(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text(
        """resources:
  west:
    provider: s3
    bucket: ridge-west
    prefix: fixtures
    region: us-west-2
  east:
    provider: s3
    bucket: ridge-east
    region: us-east-1
"""
    )

    registry = load_registry(config)

    west = registry.get("west")
    east = registry.get("east")
    assert isinstance(west, S3Resource)
    assert isinstance(east, S3Resource)
    assert west.region == "us-west-2"
    assert east.region == "us-east-1"


@pytest.mark.parametrize("missing", ["host", "root", "python"])
def test_ssh_resource_requires_contract_fields(tmp_path: Path, missing: str) -> None:
    values = {
        "host": "build.example.test",
        "root": "/workspace",
        "python": "python3",
    }
    del values[missing]
    lines = ["resources:", "  remote:", "    provider: ssh"]
    lines.extend(f"    {key}: {value}" for key, value in values.items())
    config = tmp_path / "ridge.yaml"
    config.write_text("\n".join(lines))

    with pytest.raises(ConfigurationError, match="requires"):
        load_registry(config)
