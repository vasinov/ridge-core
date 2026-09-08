# Resource providers

Installed Python distributions can add providers through the
`ridge.providers` entry-point group:

```toml
[project.entry-points."ridge.providers"]
"acme.files" = "acme_ridge:create_resource"
```

The callable receives the configured name, the provider-owned mapping without
the core-owned `provider` and `lock_key` fields, and a `ProviderContext`. It returns
a resource composed from Ridge's existing capabilities:

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
rejects provider-name collisions, identity mismatches, missing or incorrectly typed
`ResourceCapabilities`, a missing/non-callable `inspect_properties`, and construction
failures while loading the inventory, regardless of permissions. The capability
collection validates its composition and required protocol members when constructed.
Loading does not invoke inspection or prove method signatures, return values, or
backend behavior; use conformance and acceptance tests for those contracts.

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

Bounded reads must enforce `max_bytes` against content, not only prior metadata,
and raise `OutputLimitExceededError` rather than returning a truncated result.
See [data semantics](concepts/resources.md#shared-data-operations-explicit-addressing).

Transfer `finish()` completes staging; only `commit()` publishes the destination,
after both endpoints have finished successfully. Establish cleanup ownership
before accepting payload. After interruption, `cancel()` stops staging or reports
uncertainty; `abort()` must not race a possibly active writer. `abort()` must not delete a
previous destination retained for recovery, or undo a published result. An
unconfirmed commit must preserve available recovery artifacts rather than retrying
publication. Report the primary failure and attach secondary cleanup/recovery
details as exception notes; CLI/MCP and jobs preserve bounded diagnostic text.
See [copy recovery](guides/copying.md) for the built-in filesystem contract.

Built-in provider names cannot be shadowed. Providers extend implementations,
not the generic operation vocabulary: they cannot redefine an operation or add
CLI commands or MCP tools dynamically. New operations require a core design
change. See [Architecture](architecture.md) for ownership and invariants.
