# Connect your agent

Connect Ridge once to a workspace, then let the agent derive task-specific access
for its children. The same resources and policy are available through MCP, CLI,
and Python; integrations do not create another authorization system.

## Choose the connection

| Surface | Use it for | Start here |
| --- | --- | --- |
| Local stdio MCP | An agent discovers resources and invokes Ridge tools | [MCP setup](mcp.md) |
| CLI | Terminal agents, scripts, and explicit process launches | [CLI reference](cli.md) |
| Python | Programmable hosts that own child lifecycle and supervision | [Python API](python-api.md) |

Install Ridge on the host that runs the frontend and select the workspace with an
absolute configuration path. That host needs the existing backend access and
must remain available for its background work. Resources themselves can be remote.

## Give each child its own access

The parent agent calls `create_scope` with the resources and operations needed
for a task. The harness binds a separate child connection using the returned
handle through `RIDGE_SCOPE_TOKEN` or a protected token file. The child verifies
its identity with `inspect_access` before work.

A shared operator connection does not become scoped because a prompt names a
subagent. The host must support a distinct binding per child. Read
[delegating work](guides/delegation.md) for lifecycle and
[the runnable handoff](examples/delegation.md) for process plumbing.

## Client coverage

Separate Codex CLI processes have exercised scoped MCP tasks, denied operations,
reconnect, revocation, and parent supervision. The [MCP reference](mcp.md) contains
the connection recipe.

Desktop-client installation/UI behavior, other harnesses' native child-binding
mechanisms, and framework-specific adapters are not established by that test.
Direct MCP compatibility alone does not prove per-child binding. Client-specific
packaging can reuse the installed server and existing setup skill; it does not
replace Ridge's permission checks.

## Setup and troubleshooting

The [ridge-setup skill](configuration.md#agent-assisted-setup) helps an agent
configure a workspace or derive access in an existing one. It is repository-owned,
not automatically installed by the Python package.

- **Configuration rejected:** run `ridge config validate` in operator mode.
  This validates the loader, not backend connectivity.
- **Wrong task or resources:** inspect the child's effective scope. Check its
  launch environment/token-file selection; do not clear a failed binding.
- **Tool approval denied:** the host may have stopped the call before Ridge.
  Host approval and Ridge permission are separate.
- **Resource busy:** inspect [claims and reservations](guides/coordination.md);
  a different child name does not create an independent resource.
- **Job no longer visible:** verify the scope is active and has the required
  grants. An authorized parent can supervise closed descendants' jobs.
