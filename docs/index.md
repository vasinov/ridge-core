# Ridge — a resource mesh for AI agents

Connect your resources. Let agents work across them. Delegate access as the work grows.

Ridge gives agents a consistent interface to local projects, Docker containers,
SSH machines, and S3 storage. Agents discover resources, move data, run commands,
and collect results through MCP, the CLI, or Python.

When work needs several agents, the parent selects each child's resources,
operations, and output locations. The harness launches workers and binds their
connections; Ridge enforces access and coordinates participating resource calls.
Start with [installation and a local workflow](getting-started.md), then
[connect your agent](integrations.md).

## From a request to delegated work

Consider an agent comparing two approaches to a data-analysis problem. The code
lives locally, the dataset is in cloud storage, and two remote workers are ready
to run the evaluations.

> Compare these approaches. Have an agent evaluate each against the dataset,
> save their results, and tell me which performs better.

The parent agent discovers available resources, derives a scope for each child,
and has its harness launch the children with those bindings. Each child receives
read access to the inputs, its own worker, and a separate results location.
The parent follows their jobs, inspects command outcomes and reports, and closes
their access scopes after collecting the results.

The operator configures the initial authority—not every child task.
[Delegating work](guides/delegation.md) explains the agent's workflow;
the [runnable handoff](examples/delegation.md) demonstrates the process boundary
without requiring a model account.

## What Ridge handles

- **Find usable resources.** Discover named resources and their supported and
  allowed operations through a consistent interface.
- **Keep artifacts out of model context.** Stream inputs and results; read back
  what matters.
- **Delegate access, not just instructions.** Select resources, operations, and
  narrower data locations for each child, with optional expiry or further delegation.
- **Pick up work later.** Durable background jobs support reconnect, inspection,
  logs, and parent supervision while the Ridge host remains available for execution.
- **Coordinate shared updates.** Publish different files concurrently in supported
  filesystem cases, or reserve files, trees, and exact S3 objects across calls.
  See the [coordination guide](guides/coordination.md) for coverage and limits.

## Define a workspace. Connect an agent.

A **workspace** defines resource inventory, policy, and shared managed state.
Configure it in YAML using existing resources and the operations you allow.

- [Configure resources and policy](configuration.md), with agent-assisted setup.
- [Connect your agent](integrations.md) through MCP, CLI, or Python.
- [Delegate work](guides/delegation.md) within the workspace's authority.
- [Supervise background jobs](guides/jobs.md) and [coordinate shared resources](guides/coordination.md).

For a first local run, the [quickstart](getting-started.md) includes everything
needed for a small analysis. The [examples](examples/index.md) extend the same
operations to delegated tasks, builds, science, and media processing.

See the [security model](security.md) for the operating boundary,
[resource guides](resources/local.md) for backend behavior, and
[provider API](providers.md) for extensions.
