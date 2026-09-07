from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

import pytest

from ridge.application import RidgeService
from ridge.config import load_registry
from ridge.conformance import check_filesystem_capability
from ridge.errors import (
    ConfigurationError,
    OutputLimitExceededError,
    PathNotFoundError,
    UnsupportedOperationError,
)
from ridge.model import FileStat, ListEntry, Operation, ResourceProperty
from ridge.provider import ProviderContext, ResourceProviderRegistry
from ridge.resource import ResourceCapabilities


@dataclass
class _MemoryFilesystem:
    content: dict[str, bytes] = field(default_factory=dict[str, bytes])

    def list(self, path: str = ".") -> tuple[ListEntry, ...]:
        prefix = "" if path == "." else f"{path.rstrip('/')}/"
        return tuple(
            ListEntry(key, "file", len(value))
            for key, value in sorted(self.content.items())
            if key.startswith(prefix)
        )

    def read(self, path: str, *, max_bytes: int | None = None) -> bytes:
        try:
            content = self.content[path]
        except KeyError as exc:
            raise PathNotFoundError(f"path does not exist: {path}") from exc
        if max_bytes is not None and len(content) > max_bytes:
            raise OutputLimitExceededError("fixture result exceeds limit")
        return content

    def write(self, path: str, content: bytes) -> None:
        self.content[path] = content

    def stat(self, path: str) -> FileStat:
        content = self.read(path)
        return FileStat(path=path, kind="file", size=len(content), modified_ns=0)


class _FixtureResource:
    provider_name = "fixture.memory"

    def __init__(self, name: str, label: str) -> None:
        self.name = name
        self.filesystem = _MemoryFilesystem()
        self.capabilities = ResourceCapabilities(filesystem=self.filesystem)
        self._label = label

    def inspect_properties(self) -> Mapping[str, ResourceProperty]:
        return {"label": ResourceProperty(self._label, "configured")}


def test_installed_provider_constructs_composed_capability_without_core_dispatch(
    tmp_path: Path,
) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text(
        "resources: {memory: {provider: fixture.memory, label: external}}",
        encoding="utf-8",
    )
    observed: list[tuple[Mapping[str, object], ProviderContext]] = []
    created: list[_FixtureResource] = []

    def provider(
        name: str, config: Mapping[str, object], context: ProviderContext
    ) -> _FixtureResource:
        observed.append((config, context))
        label = config.get("label")
        if not isinstance(label, str):
            raise ConfigurationError("label is required")
        resource = _FixtureResource(name, label)
        created.append(resource)
        return resource

    providers = ResourceProviderRegistry()
    providers.register("fixture.memory", provider)
    service = RidgeService(load_registry(config, providers=providers))

    service.write_data("memory", "result.bin", b"first")
    service.write_data("memory", "result.bin", b"replacement")

    assert service.read_data("memory", "result.bin") == b"replacement"
    inspection = service.inspect_resource("memory")
    assert inspection.provider == "fixture.memory"
    assert inspection.addressing == "filesystem"
    assert not inspection.supports_copy
    with pytest.raises(UnsupportedOperationError, match="compute.exec"):
        service.execute("memory", ["ignored"])
    with pytest.raises(UnsupportedOperationError, match="streamed copy"):
        service.copy("memory:result.bin", "memory:copy.bin")
    assert [operation.value for operation in inspection.supported_operations] == [
        "data.list",
        "data.read",
        "data.write",
        "data.stat",
    ]
    assert inspection.properties["label"].value == "external"
    assert observed == [({"label": "external"}, ProviderContext(config))]
    check_filesystem_capability(created[0].filesystem)


def test_provider_registration_rejects_collisions() -> None:
    providers = ResourceProviderRegistry()
    providers.register("fixture.memory", lambda name, config, context: _FixtureResource(name, "a"))

    with pytest.raises(ConfigurationError, match="duplicate provider"):
        providers.register(
            "fixture.memory", lambda name, config, context: _FixtureResource(name, "b")
        )


def test_capability_collection_rejects_invalid_implementations() -> None:
    with pytest.raises(TypeError, match="filesystem capability"):
        ResourceCapabilities(filesystem=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="one data addressing model"):
        ResourceCapabilities(filesystem=_MemoryFilesystem(), storage=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="transfer requires a data capability"):
        ResourceCapabilities(transfer=object())  # type: ignore[arg-type]


def test_provider_registry_discovers_python_entry_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry_point = metadata.EntryPoint(
        name="local",
        value="ridge.backends.providers:local_provider",
        group="ridge.providers",
    )

    def entry_points(**kwargs: object) -> tuple[metadata.EntryPoint, ...]:
        assert kwargs == {"group": "ridge.providers"}
        return (entry_point,)

    monkeypatch.setattr(metadata, "entry_points", entry_points)
    providers = ResourceProviderRegistry()

    providers.discover()
    resource = providers.create(
        "local",
        "external",
        {"root": "."},
        ProviderContext(tmp_path / "ridge.yaml"),
    )

    assert resource.name == "external"
    assert resource.capabilities.compute is not None
    assert resource.capabilities.filesystem is not None


def test_provider_must_preserve_configured_resource_identity(tmp_path: Path) -> None:
    providers = ResourceProviderRegistry()
    providers.register(
        "fixture.memory", lambda name, config, context: _FixtureResource("different", "bad")
    )

    with pytest.raises(ConfigurationError, match="expected 'memory'"):
        providers.create(
            "fixture.memory",
            "memory",
            {},
            ProviderContext(tmp_path / "ridge.yaml"),
        )


def test_unknown_and_failing_providers_are_configuration_errors(tmp_path: Path) -> None:
    config = tmp_path / "ridge.yaml"
    config.write_text("resources: {item: {provider: missing.provider}}", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="unsupported provider"):
        load_registry(config, providers=ResourceProviderRegistry())

    def failing_provider(
        name: str, config: Mapping[str, object], context: ProviderContext
    ) -> _FixtureResource:
        del name, config, context
        raise RuntimeError("provider exploded")

    providers = ResourceProviderRegistry()
    providers.register("missing.provider", failing_provider)
    with pytest.raises(ConfigurationError, match="provider exploded"):
        load_registry(config, providers=providers)


def test_operations_expose_effect_metadata() -> None:
    assert Operation.DATA_READ.effect == "read"
    assert Operation.DATA_WRITE.effect == "write"
    assert Operation.COMPUTE_EXEC.effect == "execute"
    assert not Operation.COMPUTE_EXEC.idempotent
    assert Operation.DATA_WRITE.idempotent
