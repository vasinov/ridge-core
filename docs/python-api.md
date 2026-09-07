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
