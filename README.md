# Ridge

[![Tests](https://github.com/vasinov/ridge-core/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/vasinov/ridge-core/actions/workflows/tests.yml)
[![Documentation](https://github.com/vasinov/ridge-core/actions/workflows/docs.yml/badge.svg?branch=main)](https://vasinov.github.io/ridge-core/)

Ridge lets AI agents discover resources, run commands, and copy data across
local directories, existing Docker containers, SSH hosts, and S3—through one
CLI or local MCP server. Agents use resource names instead of backend-specific
transfer commands. Copies stream through Ridge without putting file contents
in model context.

Ridge also gives multiple agents a shared resource-locking protocol. An agent
copying files, editing data, or running a command can exclude conflicting Ridge
operations on the same resource. Multi-resource sessions protect a sequence of
calls, and adopting Python/MCP hosts can renew them automatically. Callers must
share local Ridge state and matching resource lock keys; this does not lock out
direct access outside Ridge. See [multi-agent coordination](docs/guides/coordination.md).

Ridge is public-alpha software for one trusted operator with multiple cooperating
agents. Exact operation grants control access through Ridge; OS and service
permissions control downstream authority. See the [security model](docs/security.md)
when choosing resources and grants.

## Install

The distribution is named `ridge-core`; the Python package and command remain
`ridge`. Install from a source checkout with Python 3.11 or newer on a POSIX host:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

These commands assume a POSIX shell at the checkout root. For development,
use `uv sync` and `. .venv/bin/activate` instead.

## Five-minute example: a sales report

From the checkout root with the environment active, copy a small CSV and
standard-library analysis script to a named worker, run it, and retrieve a report:

```bash
mkdir -p ridge-demo/inputs ridge-demo/worker ridge-demo/reports
cp docs/examples/assets/ridge.yaml ridge-demo/ridge.yaml
cp docs/examples/assets/sales.csv docs/examples/assets/analyze.py ridge-demo/inputs/
cd ridge-demo
ridge resources
ridge copy inputs:sales.csv worker:sales.csv
ridge copy inputs:analyze.py worker:analyze.py
ridge exec worker -- python3 analyze.py
ridge copy worker:report.csv reports:report.csv
ridge read reports report.csv
```

Execution and the final read print:

```text
region,revenue
East,100.00
West,200.00
```

Use a fresh demo directory. The bundled inventory defines three local resource
names with explicit grants; no cloud credentials or container are needed.
The [complete walkthrough](docs/getting-started.md) explains each step.
Switch the worker to Docker without changing the workflow in the
[cross-backend example and without-Ridge comparison](docs/examples/csv-report.md).

Configuration is loaded from `./ridge.yaml` by default. Pass `--config PATH`
or set `RIDGE_CONFIG` to select another file. Relative resource roots are
resolved relative to the configuration file.

Background mode on `exec`, `write`, and `copy` returns a durable job ID for
inspection, logs, and cancellation requests. Jobs start immediately with one
attempt. See [background jobs](docs/guides/jobs.md), including
interrupted submission and cancellation behavior.

An optional top-level `permissions` map enables default-deny exact operation
grants. Without it, Ridge runs in unrestricted trusted mode. Resource discovery
remains visible and reports supported and allowed operations; see
[Authorization](docs/concepts/authorization.md) for the boundary and bypass
model.

Commands are argument vectors and never invoke a shell implicitly. Resource
filesystem paths are relative to their configured roots; absolute paths and
paths resolving outside the root are rejected.
Execution has no timeout unless the caller explicitly requests one.

## MCP

`ridge-mcp` serves the same inventory and application operations over local
stdio using the official MCP Python SDK:

```toml
[mcp_servers.ridge]
command = "/path/to/environment/bin/ridge-mcp"
args = ["--config", "/path/to/ridge.yaml"]
required = true
default_tools_approval_mode = "writes"
```

MCP inline reads and execution output are bounded for model context. Resource
discovery grows with the inventory; job discovery returns bounded summary pages.
History storage and discovery scan cost can still grow. Tool annotations are
descriptive host hints; Ridge permission checks occur in the application service
shared with the CLI. The server has no network listener and inherits the same
downstream authority as the CLI.

## Capabilities

| Capability | Built-in resources | Operations |
| --- | --- | --- |
| Compute | local, Docker, SSH | argument-vector execution |
| Data | local, Docker, SSH, S3 | list, read, write, stat; explicit filesystem/object addressing |
| Copy workflow | all built-ins | streamed file/object copy; filesystem-only tree copy |

Configuration selects a `provider`; permissions select allowed operations.
Use `local` with data-only grants for files-only access. Discovery reports
addressing and copy support alongside supported and allowed operations.

Foreground and background copy stream bytes through Ridge with bounded payload
memory, outside model context. Direct reads and writes are buffered. See
[copy semantics](docs/guides/copying.md) for staging and replacement behavior.

Installed Python packages can add [resource providers](docs/providers.md).

## Why use Ridge?

- **Coordinate multiple agents on shared resources:** automatic resource locks
  reject conflicting CLI/MCP operations; explicit sessions reserve resources
  across a read/edit/test or copy/run/retrieve workflow. Managed caller sessions
  renew leases without asking the model to remember deadlines.
- **Less transfer glue and fewer tokens spent on it:** a named-resource copy
  avoids asking the model to generate, write, debug, and explain backend-specific
  transfer scripts. You still supply the analysis program.
- **Payloads stay out of model context:** foreground and background copy stream
  through Ridge with bounded payload memory; the caller receives metadata.
- **Discoverable operations and permissions:** inspect what each resource
  supports and what Ridge policy allows before acting.
- **One contract across CLI and MCP:** the same application authorization and
  copy semantics serve both frontends.
- **Reconnectable work:** background job IDs, results, and bounded log reads
  survive the submitting client. See the [current job limits](docs/guides/jobs.md).

See [Examples](docs/examples/index.md) for data analysis, ML experiments,
builds/tests, scientific computing, and media processing.

## Documentation

Browse the [documentation website](https://vasinov.github.io/ridge-core/).
The documentation source is in [`docs/`](docs/index.md):

- [Getting started](docs/getting-started.md)
- [Configuration](docs/configuration.md)
- [CLI](docs/cli.md) and [MCP](docs/mcp.md)
- [Copy semantics](docs/guides/copying.md)
- [Multi-agent locking and recovery](docs/guides/coordination.md)
- [Background jobs](docs/guides/jobs.md)
- [Architecture](docs/architecture.md)
- [Resource providers](docs/providers.md)
- [Python API](docs/python-api.md)
- [Security model](docs/security.md)

## Development

See [Development](docs/development.md) for setup, tests, quality checks, and
documentation builds. Feedback is especially useful on real workflows, confusing
resource semantics, and failures; include your provider, a minimal reproduction,
and expected versus actual behavior, with credentials and private details removed.

Ridge is licensed under the [Apache License 2.0](LICENSE).
