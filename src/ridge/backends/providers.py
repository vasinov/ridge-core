from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from ridge.backends.docker import DockerResource
from ridge.backends.local import LocalResource
from ridge.backends.s3 import S3Resource
from ridge.backends.ssh import SshResource
from ridge.errors import ConfigurationError
from ridge.model import PropertyScalar
from ridge.provider import ProviderContext, ResourceProviderRegistry
from ridge.resource import Resource

_COMMON_KEYS = {"root", "properties"}


def _reject_unknown(
    name: str,
    config: Mapping[str, object],
    allowed: set[str],
) -> None:
    unknown = set(config) - allowed
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise ConfigurationError(f"unknown keys for resource {name!r}: {joined}")


def _properties(name: str, value: object) -> Mapping[str, PropertyScalar]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"resource {name!r}.properties must be a mapping")
    untyped = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in untyped):
        raise ConfigurationError(f"resource {name!r}.properties keys must be strings")
    properties: dict[str, PropertyScalar] = {}
    for raw_key, item in untyped.items():
        key = cast(str, raw_key)
        if not isinstance(item, str | int | float | bool | type(None)):
            raise ConfigurationError(f"resource {name!r}.properties.{key} must be a scalar value")
        properties[key] = item
    return properties


def _string(
    name: str,
    config: Mapping[str, object],
    key: str,
    *,
    required: bool = False,
    default: str | None = None,
) -> str | None:
    value = config.get(key, default)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        requirement = "requires a non-empty string" if required else "must be a non-empty string"
        raise ConfigurationError(f"resource {name!r} {requirement} {key}")
    return value


def _local_root(
    name: str,
    config: Mapping[str, object],
    context: ProviderContext,
) -> Path:
    raw = _string(name, config, "root", required=True, default=".")
    assert raw is not None
    root = Path(raw).expanduser()
    return root if root.is_absolute() else context.config_dir / root


def _configured_file(
    name: str,
    config: Mapping[str, object],
    key: str,
    context: ProviderContext,
) -> Path | None:
    raw = _string(name, config, key)
    if raw is None:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = context.config_dir / path
    path = path.resolve()
    if not path.is_file():
        raise ConfigurationError(f"resource {name!r} {key} does not exist or is not a file: {path}")
    return path


def local_provider(name: str, config: Mapping[str, object], context: ProviderContext) -> Resource:
    _reject_unknown(name, config, _COMMON_KEYS)
    return LocalResource(
        name,
        _local_root(name, config, context),
        _properties(name, config.get("properties")),
    )


def docker_provider(name: str, config: Mapping[str, object], context: ProviderContext) -> Resource:
    del context
    _reject_unknown(name, config, _COMMON_KEYS | {"container", "python", "executable"})
    root = _string(name, config, "root", required=True)
    container = _string(name, config, "container", required=True)
    python_executable = _string(name, config, "python", required=True)
    docker_executable = _string(name, config, "executable", default="docker")
    assert root is not None and container is not None and python_executable is not None
    assert docker_executable is not None
    return DockerResource(
        name,
        container=container,
        root=root,
        python_executable=python_executable,
        docker_executable=docker_executable,
        configured_properties=_properties(name, config.get("properties")),
    )


def ssh_provider(name: str, config: Mapping[str, object], context: ProviderContext) -> Resource:
    _reject_unknown(
        name,
        config,
        _COMMON_KEYS
        | {"host", "user", "port", "python", "identity_file", "known_hosts_file", "executable"},
    )
    root = _string(name, config, "root", required=True)
    host = _string(name, config, "host", required=True)
    user = _string(name, config, "user")
    python_executable = _string(name, config, "python", required=True)
    ssh_executable = _string(name, config, "executable", default="ssh")
    port = config.get("port")
    if port is not None and (
        isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
    ):
        raise ConfigurationError(f"resource {name!r} port must be an integer between 1 and 65535")
    assert root is not None and host is not None and python_executable is not None
    assert ssh_executable is not None
    return SshResource(
        name,
        host=host,
        user=user,
        port=port,
        root=root,
        python_executable=python_executable,
        identity_file=_configured_file(name, config, "identity_file", context),
        known_hosts_file=_configured_file(name, config, "known_hosts_file", context),
        ssh_executable=ssh_executable,
        configured_properties=_properties(name, config.get("properties")),
    )


def s3_provider(name: str, config: Mapping[str, object], context: ProviderContext) -> Resource:
    del context
    _reject_unknown(name, config, {"bucket", "prefix", "region", "properties"})
    bucket = _string(name, config, "bucket", required=True)
    prefix = _string(name, config, "prefix")
    region = _string(name, config, "region")
    assert bucket is not None
    return S3Resource(
        name,
        bucket=bucket,
        prefix=prefix,
        region=region,
        configured_properties=_properties(name, config.get("properties")),
    )


def default_provider_registry() -> ResourceProviderRegistry:
    registry = ResourceProviderRegistry()
    registry.register("local", local_provider)
    registry.register("docker", docker_provider)
    registry.register("ssh", ssh_provider)
    registry.register("s3", s3_provider)
    registry.discover()
    return registry
