# Ridge

[![Tests](https://github.com/vasinov/ridge-core/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/vasinov/ridge-core/actions/workflows/tests.yml)
[![Documentation](https://github.com/vasinov/ridge-core/actions/workflows/docs.yml/badge.svg?branch=main)](https://vasinov.github.io/ridge-core/)

### A resource mesh for AI agents

Give your agent resources. Let it build the team.

Ridge connects AI agents to resources such as local projects, remote compute,
containers, and cloud storage. Agents can delegate tailored access to subagents,
coordinate shared work, and bring back results—all within permissions you establish.

Configure access once. Let agents divide it up for the task.

## From one request to a team of agents

An agent can use Ridge to split a project across subagents, giving each access to
the resources it needs. Consider comparing two approaches to a data-analysis
problem: the code lives locally, the dataset is in cloud storage, and execution
happens on remote workers.

You ask the main agent:

> Compare these two approaches. Have an agent evaluate each against the dataset,
> save their results, and tell me which performs better.

The agent discovers the available resources and delegates access:

| | Agent A | Agent B |
| --- | --- | --- |
| Project | Read access | Read access |
| Dataset | Read access | Read access |
| Compute | Worker A | Worker B |
| Results | Write under `comparison/a` | Write under `comparison/b` |

No separate inventory for each agent. No manual permission edits between tasks.

The parent creates each child's access through Ridge:

```python
# Parent agent's Ridge MCP call:
create_scope(
    grants=[
        {
            "resource": "project",
            "operations": ["data.read", "data.stat"],
        },
        {
            "resource": "datasets",
            "operations": ["data.read", "data.stat"],
        },
        {
            "resource": "worker-a",
            "operations": [
                "compute.exec",
                "data.read",
                "data.write",
                "data.stat",
            ],
        },
        {
            "resource": "results",
            "operations": ["data.read", "data.write", "data.stat"],
            "data_root": "comparison/a",
        },
    ]
)
```

The agent harness launches the child with the returned access handle bound to its
Ridge connection. The parent repeats this for Agent B with its own worker and
results prefix.

Each child copies its inputs, runs its evaluation, and publishes results. Within
Agent A's view, saving `results:metrics.json` writes to
`comparison/a/metrics.json`.

The parent follows their background jobs, compares the reports, and revokes task
access when finished. Children can delegate further when the parent allows it.

The example assumes configured resources, evaluation code, and a policy permitting
delegation. Ridge manages resource access and coordination; the agent harness
handles spawning and dispatch. Follow the
[delegation guide](https://vasinov.github.io/ridge-core/guides/delegation/) or try
the [runnable handoff example](https://vasinov.github.io/ridge-core/examples/delegation/).

## Why Ridge?

- **Delegate access, not just instructions.** Give subagents selected resources,
  permissions, and narrower data locations. They receive task-specific
  access—not the parent's entire authority.
- **Bring different resources into one workflow.** Agents work with named resources
  across machines and services without assembling backend-specific glue for every task.
- **Keep artifacts out of model context.** Stream datasets, source trees, models,
  and reports between resources. Agents read back what matters.
- **Coordinate shared work.** Ridge prevents conflicting participating operations
  from overlapping. Independent S3 objects can be accessed concurrently; sessions
  reserve resources across multi-step workflows.
- **Pick up work later.** Background jobs have durable IDs, results, and logs.
  Reconnect to inspect progress, and let parents supervise delegated work.
- **Keep control as work evolves.** Bound further delegation, set task expiry,
  or revoke access. Closing access stops new work; running jobs can be cancelled
  separately.

## A workspace for your agents

A workspace defines your resource mesh: the resources agents can access, the
permissions they can delegate, and the shared state that coordinates their work.

Define it in YAML. Resources can span machines and services; participating agents
share the workspace rather than creating separate configurations for every child.
There is no separate mesh object to create.

**You establish the initial authority. Agents decide how to divide it.**

Run `ridge config validate` to check a configuration, or use the
[ridge-setup skill](skills/ridge-setup/SKILL.md) for agent-assisted setup.

## Install and connect

From a source checkout, with Python 3.11+ on macOS or Linux:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

Use Ridge directly:

```bash
ridge --config /path/to/ridge.yaml resources
```

Or connect an agent's MCP client to:

```bash
/path/to/environment/bin/ridge-mcp --config /path/to/ridge.yaml
```

The CLI, MCP server, and Python API share the same resource access and coordination.

See [integrations](https://vasinov.github.io/ridge-core/integrations/) for connection
options, or try the
[runnable local workflow](https://vasinov.github.io/ridge-core/getting-started/).

## Bring the resources you already have

| Resource | Compute | Data operations | Streamed copy |
| --- | --- | --- | --- |
| Local | ✓ | ✓ | Files and trees |
| Docker | ✓ | ✓ | Files and trees |
| SSH | ✓ | ✓ | Files and trees |
| S3 | — | ✓ | Objects |

Data operations include listing, reading, writing, metadata inspection, and deletion.
Installed Python packages can add
[resource providers](https://vasinov.github.io/ridge-core/providers/).

Ridge governs participating calls within a shared workspace. Native OS and service
permissions govern the underlying infrastructure; delegated data roots do not
restrict arbitrary compute execution. See the
[security model](https://vasinov.github.io/ridge-core/security/) for details.

## Explore

[Documentation](https://vasinov.github.io/ridge-core/) ·
[Configuration](https://vasinov.github.io/ridge-core/configuration/) ·
[MCP](https://vasinov.github.io/ridge-core/mcp/) ·
[CLI](https://vasinov.github.io/ridge-core/cli/) ·
[Python API](https://vasinov.github.io/ridge-core/python-api/) ·
[Development](https://vasinov.github.io/ridge-core/development/)

Built for agents that do real work on real resources. Feedback, integrations,
and ambitious workflows welcome.
