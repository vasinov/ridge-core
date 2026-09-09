# Ridge — a resource mesh for AI agents

Give your agent resources. Let it build the team.

Ridge works with **Codex, Claude Code, and agents built with LangChain**, connecting
them to resources such as local projects, remote compute, containers, and cloud storage.

Your agent discovers what's available, delegates tailored access to subagents,
and brings their results together. Each child gets the resources and permissions
its task needs, while Ridge coordinates shared work and keeps access within the
boundaries you establish.

**Configure access once. Let agents divide it up for the task.**

Connect through MCP, bundle Ridge's skill and tools as a local plugin, or build
on its Python API. [Connect your agent →](integrations.md)

## From a request to delegated work

Consider an agent comparing two approaches to a data-analysis problem. The code
lives locally, the dataset is in cloud storage, and two remote workers are ready
to run the evaluations.

> Compare these approaches. Have an agent evaluate each against the dataset,
> save their results, and tell me which performs better.

The parent agent discovers available resources, derives a scope for each child,
and has its harness launch the children with those bindings. Each child receives
read access to the inputs, its own worker, and a separate results location.
The parent follows their jobs, compares reports, and closes task access.

The operator configures the initial authority—not every child task.
[Delegating work](guides/delegation.md) explains the agent's workflow;
the [runnable handoff](examples/delegation.md) demonstrates the process boundary
without requiring a model account.

## Why Ridge?

- **Delegate access, not just instructions.** Select resources, operations, and
  narrower data locations for each task, with optional further delegation.
- **Bring different resources into one workflow.** Named resources keep backend
  mechanics out of the agent's task logic.
- **Keep artifacts out of model context.** Stream inputs and results; read back
  what matters.
- **Coordinate shared work.** Reject conflicting operations and reserve resources
  across multi-step tasks. Independent S3 objects can be accessed concurrently.
- **Pick up work later.** Durable background jobs support reconnect, inspection,
  logs, and parent supervision.
- **Keep control as work evolves.** Expire or revoke task access, and cancel
  running work separately when needed.

## Define a workspace. Connect an agent.

A **workspace defines your resource mesh**: the resources agents can access, the
permissions they can delegate, and the shared state that coordinates their work.
It is configured in YAML, not a separate service or mandatory directory layout.

- [Configure resources and policy](configuration.md), with agent-assisted setup.
- [Connect your agent](integrations.md) through MCP, CLI, or Python.
- [Delegate work](guides/delegation.md) within the workspace's authority.
- [Supervise background jobs](guides/jobs.md) and [coordinate shared resources](guides/coordination.md).

For a first local run, the [quickstart](getting-started.md) includes everything
needed for a small analysis. The [examples](examples/index.md) extend the same
operations to delegated tasks, builds, science, and media processing.

Ridge manages participating resource calls; the harness owns agent spawning and
dispatch. See the [security model](security.md) for the operating boundary,
[resource guides](resources/local.md) for backend behavior, and
[provider API](providers.md) for extensions.
