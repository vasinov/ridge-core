# Ridge

Give your AI agents one way to work with local files, Docker containers, SSH
hosts, and S3. Ridge exposes named resources through a CLI and a local MCP
server: discover what is available, move data, run programs, and retrieve results.

Configure a [workspace](configuration.md#workspace): a named resource inventory,
permission policy, and managed job/coordination state shared by participating agents.
Its resources can span machines and services; no new directory layout is required.

## Start with a task

> Run the sales analysis from `inputs` on `worker`. Save the report in `reports`
> and tell me the revenue by region.

An agent discovers the resources and allowed operations, copies the inputs,
runs the analysis, checks its exit code, and reads back the small report. It
uses the same Ridge tools whether the worker is local, Docker, or SSH. File
transfers stream through Ridge instead of passing through the conversation.

Follow the [agent walkthrough](examples/csv-report.md#with-an-agent), or run the
same task yourself with the [local quickstart](getting-started.md). The
[example recipes](examples/index.md) cover ML experiments, builds, scientific
computing, and media processing.

## Why Ridge?

- **Reuse one workflow across backends.** Named resources and explicit operations
  replace per-task transfer glue and backend-specific addressing.
- **Move artifacts outside model context.** Copies relay bytes with bounded
  payload memory; agents receive metadata and choose what to read back.
- **Coordinate cooperating agents.** Resource locks reject conflicting calls;
  multi-resource sessions protect a sequence such as copy, run, and retrieve.
- **Discover access before acting.** Inspect both supported operations and
  exact operation grants through the same interface.
- **Reconnect to work.** Background execution, writes, and copies return durable
  job IDs for later inspection and bounded log reads.

Coordination requires callers to share local state and matching resource lock
keys. Managed Python/MCP caller sessions can renew reservations across long calls
and model reasoning; direct access outside Ridge is not protected. See
[coordination and host integration](guides/coordination.md).

## Connect your resources

Start with [Configuration](configuration.md) and the resource guides for
[local](resources/local.md), [Docker](resources/docker.md),
[SSH](resources/ssh.md), and [S3](resources/s3.md). Installed Python packages can
add [providers](providers.md). Filesystem and object addressing retain their own
semantics behind the shared operations.

Use the [CLI](cli.md) directly, connect an agent through [MCP](mcp.md), or embed
the [Python API](python-api.md). The frontends share application authorization,
copy semantics, jobs, and coordination.

Ridge is public-alpha software for one trusted operator with cooperating agents.
It uses the operator's existing OS and service access. Read the
[security model](security.md) when choosing resources and grants, and the
[copy](guides/copying.md) and [job](guides/jobs.md) guides for publication,
recovery, and cancellation behavior.
