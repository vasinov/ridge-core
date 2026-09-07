# Resource providers

Installed Python distributions can add providers through the
`ridge.providers` entry-point group:

```toml
[project.entry-points."ridge.providers"]
"acme.files" = "acme_ridge:create_resource"
```

The callable receives the configured name, the provider-owned mapping without
`provider`, and a `ProviderContext`. It returns a resource composed from Ridge's
existing capabilities:

```python
from collections.abc import Mapping

from ridge import FilesystemCapability, ProviderContext, ResourceCapabilities
from ridge.model import ResourceProperty


class AcmeResource:
    provider_name = "acme.files"

    def __init__(self, name: str, filesystem: FilesystemCapability) -> None:
        self.name = name
        self.capabilities = ResourceCapabilities(filesystem=filesystem)

    def inspect_properties(self) -> Mapping[str, ResourceProperty]:
        return {}


def create_resource(
    name: str,
    config: Mapping[str, object],
    context: ProviderContext,
) -> AcmeResource:
    filesystem = build_acme_filesystem(config, context.config_dir)
    return AcmeResource(name, filesystem)
```

The provider owns validation and construction of its configuration. Ridge
rejects provider-name collisions, invalid capability objects, identity
mismatches, and construction failures while loading the inventory.

The entry-point name is the YAML `provider` and must match the returned
resource's `provider_name`. Compose at most one of `filesystem` and `storage`
per resource so data requests have unambiguous addressing. Either advertises
the four `data` operations. Compute is optional. Transfer requires a data
capability and advertises `supports_copy`, not additional operation grants.

`ridge.conformance` contains reusable destructive checks for compute,
filesystem, storage, and single-file transfer implementations. Run them only
against disposable roots, prefixes, or test resources.

Providers are trusted in-process Python. They execute with Ridge's ambient
authority and are not sandboxed by Ridge request authorization.

Built-in provider names cannot be shadowed. Providers extend implementations,
not the generic operation vocabulary: they cannot redefine an operation or add
CLI commands or MCP tools dynamically. New operations require a core design
change. See [Architecture](architecture.md) for ownership and invariants.
