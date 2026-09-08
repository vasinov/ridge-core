# Ridge

Ridge gives AI agents a small, explicit interface to named compute,
filesystem, and object-storage resources. Backend mechanics stay behind one
application contract exposed through both a CLI and a local MCP server.
Resource locking lets multiple agents coordinate access to shared files and
workers without inventing their own locking protocol for every backend.

Ridge currently supports:

- local compute and rooted filesystems;
- existing Docker containers;
- POSIX hosts reached through OpenSSH;
- S3 object storage;
- streamed file and directory copy between compatible resources;
- optional exact, default-deny operation grants;
- durable immediate background jobs for execution, writes, and copy;
- coordinated access across agents, including multi-resource sessions;
- separately installed resource providers.

!!! warning "Public alpha"

    Ridge is active-development software for controlled, single-user
    environments. It inherits the operating-system authority and ambient
    credentials of its process. It is not a sandbox or a complete
    authorization boundary.

## Why Ridge?

An agent sees resource names plus supported and allowed operations rather than needing to
construct Docker, SSH, or cloud-provider commands. Ridge keeps operation
semantics consistent, rejects filesystem escapes, bounds data/execution output,
and streams cross-resource transfers without placing complete files in model
context.

**Multi-agent coordination is a core workflow.** Ordinary CLI/MCP calls acquire
resource locks automatically: reads can share access, while writes and execution
exclude conflicting operations. Explicit sessions reserve multiple resources
across a sequence of calls, such as copying inputs, running a build, and retrieving
its report. Managed Python/MCP caller sessions renew leases during long calls and
model reasoning. Shared local state and matching resource keys define the boundary;
direct access outside Ridge is not protected. Ridge does not launch agents,
assign tasks, or schedule contenders. See [resource coordination](guides/coordination.md)
for host integration, contention, and crash recovery.

Named-resource copy avoids spending model tokens generating, writing, debugging,
and explaining backend-specific transfer glue. Copy relays payloads with bounded
memory; direct reads/writes are buffered. CLI and MCP share authorization, and
durable job IDs make results and bounded logs reconnectable. Resource and job
discovery can grow with inventory/history; see the [current job limits](guides/jobs.md).

Try the [sales-report walkthrough and comparison](examples/csv-report.md), or
explore [ML, build/test, science, and media examples](examples/index.md).

```console
$ ridge resources
NAME  PROVIDER  ADDRESSING  COPY  SUPPORTED  ALLOWED  BACKGROUND
local local    filesystem  True  ...        ...      ...
$ ridge exec local -- python -c 'print("hello from Ridge")'
hello from Ridge
```

Start with [Getting started](getting-started.md), then read the
[security model](security.md) before connecting Ridge to an agent.
