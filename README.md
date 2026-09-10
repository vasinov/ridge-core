# Ridge

[![PyPI](https://img.shields.io/pypi/v/ridge-core)](https://pypi.org/project/ridge-core/)
[![Tests](https://github.com/vasinov/ridge-core/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/vasinov/ridge-core/actions/workflows/tests.yml)
[![Documentation](https://github.com/vasinov/ridge-core/actions/workflows/docs.yml/badge.svg?branch=main)](https://vasinov.github.io/ridge-core/)

### A resource mesh for AI agents

**Connect your resources. Let agents work across them. Delegate access as the work grows.**

Ridge gives agents a consistent interface to resources such as local projects,
Docker containers, SSH machines, and S3 storage. These initial providers share
capability contracts that additional providers can implement. Agents discover
what is available, move data, run commands, and collect results without assembling
a different transfer or execution interface for each backend.

When work needs more than one agent, a parent can give each worker selected
resources, operations, and output locations. Workers use the same tools and
share resource coordination. Your agent harness owns planning and worker
launching; Ridge handles resource access through **MCP, the CLI, or Python**.

[Documentation](https://vasinov.github.io/ridge-core/) ·
[Integrations](https://vasinov.github.io/ridge-core/integrations/) ·
[Examples](https://vasinov.github.io/ridge-core/examples/)

## What Ridge handles

**Find usable resources.** Discover named resources and their supported and
allowed operations instead of teaching each worker a separate backend toolset.
MCP, CLI, and Python share the same access model.

**Keep artifacts out of model context.** Stream files, source trees, datasets,
and reports between compatible resources. Agents choose the endpoints and read
back the results they need—not the entire transfer payload.

**Delegate access, not just instructions.** Give workers selected resources,
operations, and narrower data views. Bound further delegation, set optional
expiry, or revoke access without rewriting the workspace configuration.

**Pick up long-running work.** Background jobs retain IDs, status, logs, and
results across client reconnections. Authorized parents can inspect descendant
jobs; the Ridge host must remain available for execution.

**Coordinate shared updates.** Publish different files concurrently in supported
filesystem cases, or reserve files, trees, and exact S3 objects across multi-step
updates. Conflicting participating operations coordinate independently of their
access scopes. See the [coordination contract](https://vasinov.github.io/ridge-core/guides/coordination/).

## Install and connect

Install with [uv](https://docs.astral.sh/uv/getting-started/installation/)
and Python 3.11+ on macOS or Linux:

```bash
uv venv --python 3.11
. .venv/bin/activate
uv pip install ridge-core
```

For a uv-managed Python project, use `uv add ridge-core` instead.

Create a `ridge.yaml` that names your existing resources and the operations you
allow. That configuration, policy, and shared managed state form a **workspace**.
Use the [configuration guide](https://vasinov.github.io/ridge-core/configuration/)
or [agent-assisted setup](https://vasinov.github.io/ridge-core/configuration/#agent-assisted-setup).
The [local walkthrough](https://vasinov.github.io/ridge-core/getting-started/)
provides a complete example without cloud accounts.

### Connect Codex

With Codex already installed, add this entry to `~/.codex/config.toml`, or
`.codex/config.toml` in a trusted project:

```toml
[mcp_servers.ridge]
command = "/absolute/path/to/environment/bin/ridge-mcp"
args = ["--config", "/absolute/path/to/ridge.yaml"]
required = true
default_tools_approval_mode = "writes"
```

Replace both paths with your installed executable and workspace configuration.
Open a new conversation and ask:

> Inspect my Ridge access and show the available resources and operations.

Codex is one option. Connect [Claude Code or another MCP client](https://vasinov.github.io/ridge-core/integrations/clients/),
use the [LangChain integration](https://vasinov.github.io/ridge-core/integrations/langchain/),
or package the tools and setup skill as a [local plugin](https://vasinov.github.io/ridge-core/integrations/plugins/).
Use repository-owned skills and examples from the tag matching your installed release.

## From a request to coordinated work

Suppose your workspace connects a local `project`, an S3 `dataset`, two existing
remote workers, and a shared `results` destination. The evaluation code and
worker runtimes are already available. You ask:

> Compare approaches A and B against the dataset. Have a worker evaluate each,
> save their reports, and tell me which performs better. Keep the inputs unchanged.

With delegation enabled and a harness that binds each child separately, the
request can become this workflow:

| Stage | What happens |
| --- | --- |
| Discover | The parent inspects available resources and its authority through Ridge. |
| Delegate | The parent issues scopes with read-only inputs, one worker each, and separate output views. The harness launches each child with its own bound Ridge connection. |
| Execute | Children copy inputs, run evaluations, and publish reports. Ridge streams transfers, tracks background jobs, and coordinates participating operations. |
| Review | The parent inspects jobs and reports, compares the outputs, and revokes delegated access after collecting the results. |

Worker A can write `results:metrics.json` into `comparison/a/metrics.json`, while
worker B uses the same relative name under `comparison/b`. Both operate from
one inventory rather than separate workspace configurations.

One agent can use the same resource operations without delegation. For the team
workflow, MCP registration alone does not establish per-child access: the harness
must bind separate connections, and filesystem output roots must already exist.
The [agent-led Codex/Claude example](https://vasinov.github.io/ridge-core/integrations/agents/)
provides that handoff pattern.

## Initial resource providers

| Provider | Execute commands | Data operations | Streamed copy |
| --- | --- | --- | --- |
| Local | Yes | Yes | Files and trees |
| Docker | Yes | Yes | Files and trees |
| SSH | Yes | Yes | Files and trees |
| S3 | No | Yes | Objects |

Data operations include reading, writing, listing, metadata inspection, and
explicitly authorized deletion. Docker connects to an existing running container;
SSH connects to an existing host. Both require Python 3.11+ on the target.
Installed Python packages can add [providers](https://vasinov.github.io/ridge-core/providers/)
that implement the existing capability contracts.

## Operating boundaries

An access scope is an authority context, not a record of task completion.
Revocation closes future access; cancelling admitted jobs and settling
reservations are separate operations.

Ridge coordinates participating callers using the same authoritative workspace
and shared local state. Filesystem effects that cannot be safely narrowed retain
conservative protection; explicit reservations are never silently enlarged.
Arbitrary command execution remains resource-wide, and external editor or shell
effects are not inferred.

Ridge is not a sandbox, infrastructure provisioner, or agent orchestrator. Native
OS and service permissions still apply: a delegated data root narrows Ridge data
operations, not what a command can reach.

See [security](https://vasinov.github.io/ridge-core/security/),
[delegation](https://vasinov.github.io/ridge-core/guides/delegation/), and
[job lifecycle](https://vasinov.github.io/ridge-core/guides/jobs/) for details.

## Reference

[Configuration](https://vasinov.github.io/ridge-core/configuration/) ·
[MCP](https://vasinov.github.io/ridge-core/mcp/) ·
[CLI](https://vasinov.github.io/ridge-core/cli/) ·
[Python API](https://vasinov.github.io/ridge-core/python-api/) ·
[Development](https://vasinov.github.io/ridge-core/development/)

