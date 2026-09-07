# Python API

The Python package remains `ridge` even though the distribution is named
`ridge-core`.

Application code should begin with `RidgeService`:

```python
from ridge import RidgeService

ridge = RidgeService.from_config("ridge.yaml")

for resource in ridge.list_resources():
    print(resource.name, resource.supported_operations, resource.allowed_operations)

ridge.copy("project-files:README.md", "artifacts:README.md")
page = ridge.list_data("artifacts", limit=20)
```

The top-level `ridge` exports are the supported convenience API. The
`ridge.resource`, `ridge.provider`, `ridge.model`, and `ridge.conformance`
modules are also public extension surfaces for provider authors. Other modules,
including `ridge.backends`, are implementation details.

Use `RidgeService.from_config()` when permissions in the YAML document must be
applied. `load_registry()` intentionally loads only resource inventory;
constructing a service directly from that registry selects unrestricted mode.

`from_config()` also enables shared durable coordination. A directly constructed
service without a job manager has no coordination store. For multi-call work,
use `acquire_locks([JobScope(resource, Operation.DATA_WRITE), ...])` and pass the
returned token to `with_lock(token)`. That returns a separate service view, so
concurrent callers do not mutate one another's session selection. Close ownership
with `release_locks(token)`; see [session semantics](guides/coordination.md).

## Application service

::: ridge.application.RidgeService
    options:
      members: true

## Authorization

::: ridge.authorization.AuthorizationPolicy

::: ridge.authorization.AuthorizationRequest

::: ridge.authorization.Authorizer

## Resource capabilities

::: ridge.resource.ResourceCapabilities

::: ridge.resource.ComputeCapability

::: ridge.resource.FilesystemCapability

::: ridge.resource.StorageCapability

::: ridge.resource.TransferCapability

## Provider construction

::: ridge.provider.ProviderContext

::: ridge.provider.ResourceProvider

::: ridge.provider.ResourceProviderRegistry
