from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Protocol, cast

from ridge.errors import ConfigurationError
from ridge.resource import Resource, ResourceCapabilities

PROVIDER_ENTRY_POINT_GROUP = "ridge.providers"
_PROVIDER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True, slots=True)
class ProviderContext:
    """Core-owned context supplied while a provider constructs a resource."""

    config_path: Path

    @property
    def config_dir(self) -> Path:
        return self.config_path.parent


class ResourceProvider(Protocol):
    def __call__(
        self,
        name: str,
        config: Mapping[str, object],
        context: ProviderContext,
    ) -> Resource: ...


@dataclass(frozen=True, slots=True)
class _ProviderRegistration:
    description: str
    load: Callable[[], object]


class ResourceProviderRegistry:
    """Resolves provider names to built-in or installed constructors."""

    def __init__(self) -> None:
        self._registrations: dict[str, _ProviderRegistration] = {}

    def register(self, provider_name: str, provider: ResourceProvider) -> None:
        self._register(
            provider_name,
            _ProviderRegistration(
                description=f"provider registered for {provider_name!r}",
                load=lambda: provider,
            ),
        )

    def discover(self) -> None:
        entry_points = sorted(
            metadata.entry_points(group=PROVIDER_ENTRY_POINT_GROUP),
            key=lambda item: (item.name, item.value),
        )
        for entry_point in entry_points:
            self._register(
                entry_point.name,
                _ProviderRegistration(
                    description=f"entry point {entry_point.value!r}",
                    load=entry_point.load,
                ),
            )

    def create(
        self,
        provider_name: str,
        name: str,
        config: Mapping[str, object],
        context: ProviderContext,
    ) -> Resource:
        try:
            registration = self._registrations[provider_name]
        except KeyError as exc:
            raise ConfigurationError(f"unsupported provider for {name!r}: {provider_name}") from exc
        try:
            loaded = registration.load()
        except Exception as exc:
            raise ConfigurationError(
                f"cannot load provider {provider_name!r} from {registration.description}: {exc}"
            ) from exc
        if not callable(loaded):
            raise ConfigurationError(f"provider {provider_name!r} is not callable")
        provider = cast(ResourceProvider, loaded)
        resource = provider(name, config, context)
        returned_name = getattr(resource, "name", None)
        if returned_name != name:
            raise ConfigurationError(
                f"provider {provider_name!r} returned resource "
                f"name {returned_name!r}, expected {name!r}"
            )
        returned_provider = getattr(resource, "provider_name", None)
        if returned_provider != provider_name:
            raise ConfigurationError(
                f"provider {provider_name!r} returned provider {returned_provider!r}"
            )
        if not isinstance(getattr(resource, "capabilities", None), ResourceCapabilities):
            raise ConfigurationError(
                f"provider {provider_name!r} resource {name!r} requires ResourceCapabilities"
            )
        if not callable(getattr(resource, "inspect_properties", None)):
            raise ConfigurationError(
                f"provider {provider_name!r} resource {name!r} requires callable inspect_properties"
            )
        return resource

    def _register(self, provider_name: str, registration: _ProviderRegistration) -> None:
        if not _PROVIDER_NAME.fullmatch(provider_name):
            raise ConfigurationError(f"invalid provider name: {provider_name!r}")
        existing = self._registrations.get(provider_name)
        if existing is not None:
            raise ConfigurationError(
                f"duplicate provider {provider_name!r}: "
                f"{existing.description} and {registration.description}"
            )
        self._registrations[provider_name] = registration
