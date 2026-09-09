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
`ridge config validate` uses this same construction path. Keep constructors
focused on configuration and defer backend interactions to capability methods;
provider code and provider-written diagnostics are not sandboxed or redacted.

The entry-point name is the YAML `provider` and must match the returned
resource's `provider_name`. Compose at most one of `filesystem` and `storage`
per resource so data requests have unambiguous addressing. Either advertises
the four `data` operations. Compute is optional. Transfer requires a data
capability and advertises `supports_copy`, not additional operation grants.
Optional `delete=implementation` requires one data addressing capability and
advertises `data.delete`. Implement `DeleteCapability.delete(path, recursive=False)`
returning `DeleteResult`, following [deletion semantics](concepts/resources.md#deletion).
Do not advertise deletion merely because existing write methods replace entries.

Optional `data_views=implementation` implements `DataViewCapability`:
`validate_data_root(root)` performs pure, nonconnecting syntax validation;
`open_data_view(roots)` returns narrowed data capabilities, preserving their addressing
and operation support. `roots` is the ordered chain of parent-relative boundaries.
Validate every physical boundary at use time, not just the final path. Ridge calls
this method lazily under the operation claim, including copy and admitted jobs.
Do not create directories as part of view resolution. Returned listings/metadata
use view-relative coordinates. Scope creation rejects narrowing when this capability
is absent. Compute, registry names, managed state and canonical claims are not rebased.

Optional `footprints=implementation` implements `FootprintCapability`:

- `coordinate_space()` returns an immutable tuple identifying compatible canonical
  coordinates, or `None` for unknown mapping. Every alias in the configured lock
  domain must advertise the same space before Ridge narrows any operation there.
- `plan_footprint(operation, path, roots)` returns a nonempty tuple of public
  `Footprint(scope, mode)` values, or `None` for whole-resource fallback. Include
  the complete immutable parent-relative root chain in canonical coordinates.
  The plan must cover both direct operations and corresponding transfer endpoints,
  including staging, publication, validation and cleanup. Mutating operations
  require exclusive claims. Providers cannot select another lock domain.

Both methods must be pure and nonconnecting: no mutable backend inspection,
filesystem resolution, client construction, or side effects. Ridge calls them
before admission, outside its database transaction, then rechecks scoped authority
inside admission. A scope is `None` or a nonempty tuple of nonempty opaque strings;
tuple ancestry, not text-prefix matching, defines overlap. Never claim narrow
coverage that depends on unprotected mutable state. Unsupported planning falls
back; malformed results fail. Oversize plans conservatively collapse to whole-domain
claims (64 claims/action, 32 components/scope, 16 KiB encoded scope).
Existing providers without this capability keep whole-resource locking.

`ridge.conformance` contains reusable destructive checks for compute,
filesystem, storage, deletion, and single-file transfer implementations. Run them only
against disposable roots, prefixes, or test resources.
`check_delete_capability` takes an exact disposable `path`, a write callback,
and an existence callback. It verifies removal and repeated missing deletion;
providers must additionally test recursion, boundaries, and failure semantics.

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
