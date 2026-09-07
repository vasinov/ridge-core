from pathlib import Path

import pytest

from ridge.backends.local import LocalResource
from ridge.errors import ResourceNotFoundError
from ridge.model import ResourceProperty
from ridge.registry import ResourceRegistry


def test_registry_orders_names_and_reports_unknown_resource(tmp_path: Path) -> None:
    registry = ResourceRegistry(
        [
            LocalResource("zeta", tmp_path),
            LocalResource("alpha", tmp_path),
        ]
    )

    assert registry.names() == ("alpha", "zeta")
    with pytest.raises(ResourceNotFoundError, match="missing"):
        registry.get("missing")


def test_listing_does_not_invoke_provider_inspection(tmp_path: Path) -> None:
    class InspectionFixture(LocalResource):
        def inspect_properties(self) -> dict[str, ResourceProperty]:
            raise AssertionError("listing must not inspect the backend")

    registry = ResourceRegistry([InspectionFixture("data", tmp_path)])

    listing = registry.inspections()

    assert listing[0].name == "data"
    assert listing[0].properties == {}
