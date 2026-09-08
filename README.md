# Ridge

[![Tests](https://github.com/vasinov/ridge-core/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/vasinov/ridge-core/actions/workflows/tests.yml)
[![Documentation](https://github.com/vasinov/ridge-core/actions/workflows/docs.yml/badge.svg?branch=main)](https://vasinov.github.io/ridge-core/)

Give your AI agents one way to work with local files, Docker containers, SSH
hosts, and S3. Ridge exposes named resources through a CLI and a local MCP
server, so agents can discover what is available, move data, and run programs
without assembling backend-specific plumbing.

## From a request to a result

> Run the sales analysis from `inputs` on `worker`. Save the report in `reports`
> and tell me the revenue by region.

With Ridge connected to an MCP-capable agent, a task like this becomes a short
sequence of tool calls. Assume the three resources are configured, `inputs`
contains `sales.csv` and `analyze.py`, and `worker` has Python:

```text
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
See the [agent walkthrough](docs/examples/csv-report.md#with-an-agent) for the
decision points and background-job variant.

## Why Ridge?

- **One workflow across backends.** Use `RESOURCE:PATH` locations instead of
  stitching together filesystem, Docker, SSH, and cloud transfer commands.
- **Keep artifacts out of model context.** Stream datasets, model files, and
  reports between resources with bounded transfer memory; read back what matters.
- **Give agents a shared way to coordinate.** Automatic resource locks reject
  conflicting Ridge calls; sessions reserve resources across a multi-step task.
- **Discover before acting.** Agents can inspect both supported operations and
  the operations your Ridge configuration allows.
- **Pick up work later.** Background jobs provide durable IDs, results, and logs
  that another client can inspect after submission.

Coordination requires shared local Ridge state and matching resource lock keys;
it does not exclude access outside Ridge. See [multi-agent coordination](docs/guides/coordination.md).

## The same workflow in your terminal

With the same resources and inputs already in place:

```bash
ridge resources
ridge copy inputs:sales.csv worker:sales.csv
ridge copy inputs:analyze.py worker:analyze.py
ridge exec worker --timeout 30 -- python3 analyze.py
# After a successful exit:
ridge copy worker:report.csv reports:report.csv
ridge read reports report.csv
```

```text
region,revenue
East,100.00
West,200.00
```

Ridge loads `./ridge.yaml` by default; use `--config PATH` to choose another
inventory. The [getting-started guide](docs/getting-started.md) provides a complete
local setup with bundled inputs. Explore [more examples](docs/examples/index.md)
for ML experiments, builds, scientific computing, and media processing.

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
with bounded inline reads and execution output. See [MCP setup and tools](docs/mcp.md).

## Resources and capabilities

| Capability | Built-in resources | Operations |
| --- | --- | --- |
| Compute | local, Docker, SSH | argument-vector execution |
| Data | local, Docker, SSH, S3 | list, read, write, stat |
| Copy workflow | all built-ins | streamed file/object copy; filesystem-only tree copy |

Configure providers and exact operation grants in your inventory. Installed
Python packages can add [resource providers](docs/providers.md).

Ridge is public-alpha software for one trusted operator with cooperating agents.
Ridge grants control calls through Ridge; OS and service permissions determine
downstream authority. Review the [security model](docs/security.md) when
connecting resources. [Copying](docs/guides/copying.md) and
[background jobs](docs/guides/jobs.md) cover replacement, recovery, and cancellation.

## Documentation and development

Browse the [documentation website](https://vasinov.github.io/ridge-core/), or start
with [Configuration](docs/configuration.md), [CLI](docs/cli.md),
[MCP](docs/mcp.md), and the [Python API](docs/python-api.md).
See [Architecture](docs/architecture.md) for the design and
[Development](docs/development.md) for setup and verification.

Feedback on real agent workflows, confusing resource semantics, and failures is
welcome. Include your provider, a minimal reproduction, and expected versus
actual behavior, with credentials and private details removed.

Ridge is licensed under the [Apache License 2.0](LICENSE).
