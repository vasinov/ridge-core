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

Ridge is public-alpha software for one trusted operator with multiple cooperating
agents. It uses the operator's existing OS and service access; the
[security model](security.md) explains how to choose resources and grants.

## Why Ridge?

An agent sees resource names plus supported and allowed operations rather than needing to
construct Docker, SSH, or cloud-provider commands. Ridge keeps operation
semantics consistent, checks filesystem paths against configured roots, bounds
MCP inline reads and execution output, and streams cross-resource transfers
without placing complete files in model
context.

**Multi-agent coordination is a core workflow.** Ordinary CLI/MCP calls acquire
resource locks automatically: reads can share access, while writes and execution
exclude conflicting operations. Explicit sessions reserve multiple resources
across a sequence of calls, such as copying inputs, running a build, and retrieving
its report. Managed Python/MCP caller sessions renew leases during long calls and
model reasoning. Shared local state and matching resource keys define the boundary;
direct access outside Ridge is not protected. See [resource coordination](guides/coordination.md)
for host integration, contention, and crash recovery.

Named-resource copy avoids spending model tokens generating, writing, debugging,
and explaining backend-specific transfer glue. Copy relays payloads with bounded
memory; direct reads/writes are buffered. CLI and MCP share authorization, and
durable job IDs make results and bounded logs reconnectable. Resource discovery
grows with the inventory; job discovery returns bounded summary pages, while
history storage and metadata scan cost can grow. See [job discovery](guides/jobs.md#discovering-jobs).

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
