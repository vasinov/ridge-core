from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import yaml

from ridge.authorization import AuthorizationPolicy
from ridge.backends.providers import default_provider_registry
from ridge.errors import ConfigurationError, RidgeError
from ridge.model import Operation
from ridge.provider import ProviderContext, ResourceProviderRegistry
from ridge.registry import ResourceRegistry
from ridge.resource import Resource

_RESOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True, slots=True)
class LoadedConfiguration:
    registry: ResourceRegistry
    authorization: AuthorizationPolicy
    path: Path | None = None
    state_directory: Path | None = None
    lock_keys: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    resource_identities: dict[str, str] = field(default_factory=lambda: dict[str, str]())


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{label} must be a mapping")
    untyped_mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in untyped_mapping):
        raise ConfigurationError(f"{label} keys must be strings")
    return cast(Mapping[str, object], untyped_mapping)


def load_registry(
    config_path: str | Path,
    *,
    providers: ResourceProviderRegistry | None = None,
) -> ResourceRegistry:
    return load_configuration(config_path, providers=providers).registry


def load_configuration(
    config_path: str | Path,
    *,
    providers: ResourceProviderRegistry | None = None,
    expected_resource_identities: Mapping[str, str] | None = None,
    expected_state_directory: Path | None = None,
) -> LoadedConfiguration:
    path = Path(config_path).expanduser().resolve()
    try:
        raw_bytes = path.read_bytes()
        raw_document: Any = yaml.safe_load(raw_bytes.decode("utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"configuration file does not exist: {path}") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot read configuration {path}: {exc}") from exc

    document = _mapping(raw_document, "configuration")
    unknown_document_keys = set(document) - {"resources", "permissions", "state"}
    if unknown_document_keys:
        joined = ", ".join(sorted(unknown_document_keys))
        raise ConfigurationError(f"unknown configuration keys: {joined}")
    resource_configs = _mapping(document.get("resources"), "resources")
    state_directory = _load_state(document.get("state"), path)
    if (
        expected_state_directory is not None
        and state_directory != expected_state_directory.resolve()
    ):
        raise ConfigurationError("workspace state directory changed after submission")
    resource_identities = {
        name: _resource_identity(name, _mapping(value, f"resource {name!r}"), path)
        for name, value in resource_configs.items()
    }
    if expected_resource_identities is not None:
        for name, identity in expected_resource_identities.items():
            if resource_identities.get(name) != identity:
                raise ConfigurationError(f"resource {name!r} changed after submission")
    # Identity checks precede discovery/construction of trusted provider code.
    provider_registry = providers or default_provider_registry()
    context = ProviderContext(config_path=path)
    resources: list[Resource] = []
    lock_keys: dict[str, str] = {}

    for name, raw_config in resource_configs.items():
        if not _RESOURCE_NAME.fullmatch(name):
            raise ConfigurationError(f"invalid resource name: {name!r}")
        config = _mapping(raw_config, f"resource {name!r}")
        provider_name = config.get("provider")
        if not isinstance(provider_name, str):
            raise ConfigurationError(f"resource {name!r} requires a string provider")
        lock_key = config.get("lock_key", name)
        if (
            not isinstance(lock_key, str)
            or not _RESOURCE_NAME.fullmatch(lock_key)
            or len(lock_key) > 128
        ):
            raise ConfigurationError(f"invalid lock_key for resource {name!r}")
        lock_keys[name] = lock_key
        provider_config = {
            key: value for key, value in config.items() if key not in {"provider", "lock_key"}
        }
        try:
            resources.append(
                provider_registry.create(provider_name, name, provider_config, context)
            )
        except ConfigurationError:
            raise
        except RidgeError as exc:
            raise ConfigurationError(f"invalid resource {name!r}: {exc}") from exc
        except Exception as exc:
            raise ConfigurationError(
                f"provider for resource {name!r} failed to construct it: {exc}"
            ) from exc

    registry = ResourceRegistry(resources)
    if "permissions" not in document:
        authorization = AuthorizationPolicy.unrestricted()
    else:
        authorization = _load_permissions(document["permissions"], registry)
    return LoadedConfiguration(
        registry,
        authorization,
        path,
        state_directory,
        lock_keys,
        resource_identities,
    )


def _semantic_value(value: object) -> str:
    """Canonicalize YAML values without conflating scalar types or mapping order."""
    if isinstance(value, Mapping):
        pairs = sorted(
            (_semantic_value(key), _semantic_value(item))
            for key, item in cast(Mapping[object, object], value).items()
        )
        return json.dumps(["mapping", pairs])
    if isinstance(value, (list, tuple, set)):
        container_type = (
            "set" if isinstance(value, set) else "list" if isinstance(value, list) else "tuple"
        )
        items = [_semantic_value(item) for item in cast(list[object], value)]
        return json.dumps([container_type, sorted(items) if container_type == "set" else items])
    return json.dumps([type(value).__name__, str(value)])


def _resource_identity(name: str, config: Mapping[str, object], path: Path) -> str:
    identity = dict(config)
    identity.setdefault("lock_key", name)
    try:
        encoded = _semantic_value([str(path.parent), identity])
    except RecursionError as exc:
        raise ConfigurationError(f"resource {name!r} contains recursive configuration") from exc
    return sha256(encoded.encode()).hexdigest()


def _load_state(value: object, config_path: Path) -> Path:
    if value is None:
        return (config_path.parent / ".ridge").resolve()
    config = _mapping(value, "state")
    unknown = set(config) - {"directory"}
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise ConfigurationError(f"unknown state keys: {joined}")
    raw_directory = config.get("directory", ".ridge")
    if not isinstance(raw_directory, str) or not raw_directory:
        raise ConfigurationError("state directory must be a non-empty string")
    directory = Path(raw_directory).expanduser()
    if not directory.is_absolute():
        directory = config_path.parent / directory
    return directory.resolve()


def _load_permissions(value: object, registry: ResourceRegistry) -> AuthorizationPolicy:
    permission_config = _mapping(value, "permissions")
    grants: dict[str, frozenset[Operation]] = {}
    for resource_name, raw_operations in permission_config.items():
        if resource_name not in registry.names():
            raise ConfigurationError(f"permissions reference unknown resource: {resource_name!r}")
        if not isinstance(raw_operations, list):
            raise ConfigurationError(f"permissions for resource {resource_name!r} must be a list")
        operations: list[Operation] = []
        for raw_operation in cast(list[object], raw_operations):
            if not isinstance(raw_operation, str):
                raise ConfigurationError(
                    f"permissions for resource {resource_name!r} must contain operation names"
                )
            try:
                operation = Operation(raw_operation)
            except ValueError as exc:
                raise ConfigurationError(
                    f"unknown operation in permissions: {raw_operation!r}"
                ) from exc
            if operation in operations:
                raise ConfigurationError(
                    f"duplicate permission for resource {resource_name!r}: {operation.value}"
                )
            operations.append(operation)

        supported = registry.get(resource_name).capabilities.operations
        unsupported = [operation.value for operation in operations if operation not in supported]
        if unsupported:
            joined = ", ".join(unsupported)
            raise ConfigurationError(
                f"resource {resource_name!r} does not support granted operations: {joined}"
            )
        grants[resource_name] = frozenset(operations)
    return AuthorizationPolicy.exact(grants)
