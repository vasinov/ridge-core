# Ridge

[![Tests](https://github.com/vasinov/ridge-core/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/vasinov/ridge-core/actions/workflows/tests.yml)
[![Documentation](https://github.com/vasinov/ridge-core/actions/workflows/docs.yml/badge.svg?branch=main)](https://vasinov.github.io/ridge-core/)

Give your AI agents one way to execute code and work with data across resources
such as local machines, Docker containers, SSH hosts, and S3. Ridge exposes named
resources through a CLI and a local MCP server, with extensible providers for
additional backends. It also coordinates multiple agents on shared resources
and keeps background jobs available for later inspection.

## From a request to a result

> Run the sales analysis from `inputs` on `worker`. Save the report in `reports`
> and tell me the revenue by region.

With Ridge connected to an MCP-capable agent, a task like this becomes a short
sequence of tool calls. Assume the three resources are configured, `inputs`
contains `sales.csv` and `analyze.py`, and `worker` has Python:

```python3
list_resources()
copy(source="inputs:sales.csv", destination="worker:sales.csv")
copy(source="inputs:analyze.py", destination="worker:analyze.py")
execute(resource="worker", argv=["python3", "analyze.py"], timeout_seconds=30)
# After checking result.exit_code == 0:
copy(source="worker:report.csv", destination="reports:report.csv")
read_data(resource="reports", path="report.csv")
```

Illustrative MCP calls, with responses omitted. The agent checks supported and
allowed operations before acting, then reads the small report to answer:

> East: 100.00. West: 200.00. Report saved to `reports:report.csv`.

The worker can be local, Docker, or SSH—the tool calls stay the same. Copies
stream through Ridge; input files do not need to pass through the conversation.
See the [agent walkthrough](https://vasinov.github.io/ridge-core/examples/csv-report/#with-an-agent) for the
decision points and background-job variant.

The same flow is available through the [CLI](https://vasinov.github.io/ridge-core/cli/);
the [getting-started guide](https://vasinov.github.io/ridge-core/getting-started/)
shows the commands and provides bundled inputs.

## Why Ridge?

- **One workflow across backends.** Use `RESOURCE:PATH` locations instead of
  stitching together filesystem, Docker, SSH, and cloud transfer commands.
- **Keep artifacts out of model context.** Stream datasets, model files, and
  reports between resources with bounded transfer memory; read back what matters.
- **Give agents a shared way to coordinate.** Automatic resource locks reject
  conflicting Ridge calls; sessions reserve resources across a multi-step task.
- **Discover before acting.** Agents can inspect both supported operations and
  the operations your Ridge configuration allows.
- **Delegate bounded tasks.** Derive child access to named resources and narrower
  data roots without rewriting inventories. Parents can supervise delegated jobs.
  Task handles reconnect, expire, or revoke independently
  of lock ownership. See [delegation](https://vasinov.github.io/ridge-core/concepts/authorization/#create-bind-and-close-a-task).
- **Pick up work later.** Background jobs provide durable IDs, results, and logs
  that another client can inspect after submission.
- **Clean up explicitly.** Delete exact files, directory trees, or object keys
  with a separate `data.delete` grant, in foreground or background.
  See [deletion semantics](https://vasinov.github.io/ridge-core/concepts/resources/#deletion).

Agents using the same workspace share local Ridge state and resource lock keys.
See [multi-agent coordination](https://vasinov.github.io/ridge-core/guides/coordination/).

## Configure your workspace

A **Ridge workspace** is your resource inventory, permission policy, and managed
state for jobs and coordination. Define it with a YAML configuration; it need not
be a single directory or repository. Resources can live on different machines.

Here is a local `ridge.yaml` for the example above:

```yaml
resources:
  inputs:
    provider: local
    root: ./inputs
  worker:
    provider: local
    root: ./worker
  reports:
    provider: local
    root: ./reports

permissions:
  inputs: [data.list, data.read, data.stat]
  worker: [compute.exec, data.list, data.read, data.write, data.stat]
  reports: [data.list, data.read, data.write, data.stat]
```

The directories already exist beside the configuration file. The grants allow
reading inputs, running code on the worker, and saving and reading reports.
Change the worker's provider configuration to use Docker or SSH while keeping
the resource name and agent workflow.

Ridge loads `./ridge.yaml` by default; use `--config PATH` to select a workspace's
configuration. Managed state defaults to `.ridge` beside that file. Use the same
configuration for participating agents; no workspace creation command is needed.
Run `ridge config validate` to check it and review effective grants without
running resource operations. For agent-assisted configuration, use the
[ridge-setup skill](skills/ridge-setup/SKILL.md) with installed Ridge; see
[setup and validation](https://vasinov.github.io/ridge-core/configuration/#agent-assisted-setup).
See [Configuration](https://vasinov.github.io/ridge-core/configuration/) for options
and [more examples](https://vasinov.github.io/ridge-core/examples/) for ML experiments,
builds, scientific computing, and media processing.

## Install and connect

From a source checkout, using Python 3.11+ on a POSIX host:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

The distribution is `ridge-core`; the package and command are `ridge`.
For development, use `uv sync` and activate the environment instead.

Point your agent's MCP configuration at the installed `ridge-mcp` executable and
your resource inventory. For example:

```toml
[mcp_servers.ridge]
command = "/path/to/environment/bin/ridge-mcp"
args = ["--config", "/path/to/ridge.yaml"]
required = true
default_tools_approval_mode = "writes"
```

The local stdio server exposes the same operations and authorization as the CLI,
with bounded inline reads and execution output. See [MCP setup and tools](https://vasinov.github.io/ridge-core/mcp/).

## Resources and capabilities

| Capability | Built-in resources | Operations |
| --- | --- | --- |
| Compute | local, Docker, SSH | argument-vector execution |
| Data | local, Docker, SSH, S3 | list, read, write, stat |
| Copy workflow | all built-ins | streamed file/object copy; filesystem-only tree copy |

Configure providers and exact operation grants in your inventory. Installed
Python packages can add [resource providers](https://vasinov.github.io/ridge-core/providers/).

Ridge is public-alpha software for one trusted operator with cooperating agents.
Ridge grants control calls through Ridge; OS and service permissions determine
downstream authority. Review the [security model](https://vasinov.github.io/ridge-core/security/) when
connecting resources. [Copying](https://vasinov.github.io/ridge-core/guides/copying/) and
[background jobs](https://vasinov.github.io/ridge-core/guides/jobs/) cover replacement, recovery, and cancellation.

## Documentation and development

Browse the [documentation website](https://vasinov.github.io/ridge-core/), or start
with [Configuration](https://vasinov.github.io/ridge-core/configuration/), [CLI](https://vasinov.github.io/ridge-core/cli/),
[MCP](https://vasinov.github.io/ridge-core/mcp/), and the [Python API](https://vasinov.github.io/ridge-core/python-api/).
See [Architecture](https://vasinov.github.io/ridge-core/architecture/) for the design and
[Development](https://vasinov.github.io/ridge-core/development/) for setup and verification.

Feedback on real agent workflows, confusing resource semantics, and failures is
welcome. Include your provider, a minimal reproduction, and expected versus
actual behavior, with credentials and private details removed.

Ridge is licensed under the [Apache License 2.0](LICENSE).
