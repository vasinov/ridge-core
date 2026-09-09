# A delegated task, end to end

A parent agent can derive access for children without returning to the operator for
every task. This example makes the handoff concrete: a scoped parent creates two
children, launches separate processes, supervises their background jobs, and
reconnects them to publish results before closing access.

The children are scripted so the example runs without a model account or harness
installation. It exercises real Ridge APIs, process bindings, jobs, and data
operations—not an emulated permission system. It demonstrates host plumbing,
not autonomous model planning or a benchmark of agent quality.

## Run it

From a source checkout with Ridge installed in the active Python environment:

```bash
python docs/examples/assets/delegate.py /tmp/ridge-delegation-demo
```

Choose a new destination; the script refuses an existing directory. It creates
only local fixture resources and keeps inputs, results, configuration, and job
evidence there. The [script](assets/delegate.py) uses the public Python API.

Expected summary (job IDs vary):

```json
{
  "scores": {"a": {"score": 10}, "b": {"score": 4}},
  "jobs": ["...", "..."],
  "child_scopes_closed": true
}
```

The tiny evaluation compares sum and maximum over `[1, 2, 3, 4]`. The numbers
make it easy to inspect the result; they are not performance measurements.

## What happens

1. **Bootstrap once.** Create a workspace with shared read-only inputs, two local
   workers, and results directories. The operator issues the parent agent a scope
   with delegation authority and read access to results.
2. **The parent divides access.** The scope-bound parent—not the operator—issues
   each child read access to inputs, access to one worker, and a rooted results view.
   Neither child can delegate further.
3. **The host binds separate children.** Each process receives its token through
   its environment and explicitly binds the Python service. It verifies scope
   identity and resource discovery, and checks that writing to inputs is denied.
4. **Children submit work and exit.** Each copies the inputs and submits a bounded
   execution job. The parent inspects those descendant jobs using delegation
   authority, without direct compute access.
5. **Reconnect and publish.** New child processes reuse the same handles to copy
   their metrics into their results views. Publication is sequenced because these
   local result directories share one whole-resource lock domain.
6. **Collect and close.** The parent reads the results, revokes child scopes,
   checks that closed handles fail, and verifies unchanged inputs and no remaining
   claims. The bootstrap host closes the parent scope.

Both children use the same job idempotency key; it is scoped independently.
No token is printed or placed in a subprocess argument. The example retains job
records and output but no token files.

## Adapt it to an agent harness

The agent decides how to divide tasks and invokes Ridge's scope-creation API.
The host's launch adapter supplies the resulting handle to the child's Ridge
connection. In this example, `launch()` is that adapter and `child()` is the
scripted task body; a real harness replaces the body with an agent and its tools.

MCP children bind at server startup with `RIDGE_SCOPE_TOKEN` or
`--scope-token-file`. Python binding is explicit:
`RidgeService.from_config(config, scope_token=token)`.
Do not reuse the operator's connection for a child.
See [integrations](../integrations.md) and [delegating work](../guides/delegation.md).

The local fixture needs no multi-step reservation because each worker is dedicated
to its child. For a shared worker, the child owns a
[session](../guides/coordination.md); the parent should not hold a conflicting
reservation while awaiting it. Changing a worker to an existing remote resource
also requires an appropriate target interpreter rather than this fixture's
`sys.executable`.

## Inspect the retained workspace

```bash
ridge --config /tmp/ridge-delegation-demo/ridge.yaml scope list
ridge --config /tmp/ridge-delegation-demo/ridge.yaml jobs list
ridge --config /tmp/ridge-delegation-demo/ridge.yaml read results a/metrics.json
ridge --config /tmp/ridge-delegation-demo/ridge.yaml read results b/metrics.json
ridge --config /tmp/ridge-delegation-demo/ridge.yaml locks list
```

On success, the scopes are closed, both jobs succeeded with exit code zero, and
the claim list is empty. If interrupted, inspect jobs and claims before removing
the fixture. Scope revocation is not cancellation; see
[job recovery](../guides/jobs.md#current-limitations).
