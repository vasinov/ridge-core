from __future__ import annotations

from collections.abc import Iterable

from ridge.errors import ResourceNotFoundError
from ridge.model import ResourceInspection, ResourceProperty
from ridge.resource import Resource


class ResourceRegistry:
    def __init__(self, resources: Iterable[Resource]) -> None:
        self._resources: dict[str, Resource] = {}
        for resource in resources:
            if resource.name in self._resources:
                raise ValueError(f"duplicate resource name: {resource.name}")
            self._resources[resource.name] = resource

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._resources))

    def get(self, name: str) -> Resource:
        try:
            return self._resources[name]
        except KeyError as exc:
            raise ResourceNotFoundError(f"unknown resource: {name}") from exc

    def inspect(self, name: str) -> ResourceInspection:
        resource = self.get(name)
        return self._inspection(resource, properties=dict(resource.inspect_properties()))

    @staticmethod
    def _inspection(
        resource: Resource,
        *,
        properties: dict[str, ResourceProperty],
    ) -> ResourceInspection:
        return ResourceInspection(
            name=resource.name,
            provider=resource.provider_name,
            addressing=resource.capabilities.addressing,
            supports_copy=resource.capabilities.transfer is not None,
            supported_operations=resource.capabilities.operations,
            allowed_operations=resource.capabilities.operations,
            background_operations=(),
            properties=properties,
        )

    def inspections(self) -> tuple[ResourceInspection, ...]:
        return tuple(
            self._inspection(self._resources[name], properties={}) for name in self.names()
        )
