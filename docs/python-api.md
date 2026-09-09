# Python API

The Python package remains `ridge` even though the distribution is named
`ridge-core`.

For inventory validation without application/state initialization, use the
[configuration validation CLI](configuration.md#validate-an-inventory).

Application code should begin with `RidgeService`:

```python
from ridge import RidgeService

ridge = RidgeService.from_config("ridge.yaml")

for resource in ridge.list_resources():
    print(resource.name, resource.supported_operations, resource.allowed_operations)

ridge.copy("project-files:README.md", "artifacts:README.md")
page = ridge.list_data("artifacts", limit=20)
```

Job discovery returns a `JobPage` containing `JobSummary` values:

```python
page = ridge.list_jobs(limit=20)
for job in page.jobs:
    print(job.id, job.status)
if page.next_cursor is not None:
    page = ridge.list_jobs(limit=20, cursor=page.next_cursor)
```

Use `ridge.inspect_job(job_id)` for results and errors. See
[job discovery](guides/jobs.md#discovering-jobs) for ordering, bounds, and policy changes.

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

Prefer `with ridge.lock_session(scopes) as session:` for host-owned workflows;
`session.service` attaches the token and a background thread renews the lease.
`ManagedMCPSession(client, scopes)` provides an asyncio context with
`session.call_tool(...)` for MCP hosts. Both are single-use and fail closed on
renewal failure. See [managed caller sessions](guides/coordination.md#managed-caller-sessions)
for lifecycle, cancellation, and integration requirements.

## Delegated tasks

```python
from ridge import AccessGrant, Operation, RidgeService

operator = RidgeService.from_config("ridge.yaml")
issued = operator.create_scope(
    [
        AccessGrant("inputs", frozenset({Operation.DATA_READ, Operation.DATA_STAT})),
    ]
)
child = RidgeService.from_config("ridge.yaml", scope_token=issued.token)
print(child.inspect_access())
operator.revoke_scope(issued.scope.id)
```

The workspace must permit and explicitly delegate these operations. Store the
returned token securely; it is not returned by inspection. Bound service methods
reload configuration and recheck lifecycle for each request. Python binding is
explicit: `from_config()` does not read `RIDGE_SCOPE_TOKEN`; CLI/MCP entry points
own environment/file transport. `None` deliberately selects operator mode in trusted
Python code; empty or invalid strings fail closed.
See [task access](concepts/authorization.md#create-bind-and-close-a-task) for
attenuation, expiry, visibility, and the distinction from lock sessions.

`AccessGrant(..., data_root="outputs/task-a")` narrows data access relative to the
parent view. The directory is validated when used, not created at issuance.
Omitting `data_root` inherits the parent's view; compute is not narrowed.

## Managed sessions

::: ridge.ManagedSession

::: ridge.ManagedMCPSession

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

::: ridge.resource.DeleteCapability

::: ridge.model.DeleteResult

## Provider construction

::: ridge.provider.ProviderContext

::: ridge.provider.ResourceProvider

::: ridge.provider.ResourceProviderRegistry
