# MCP server

`ridge-mcp` exposes the configured inventory over local stdio using the
official MCP Python SDK. It has no network listener.

Background cancellation records durable intent and survives a client disconnect.
Inspect the returned job: `cancelled` confirms owned-local-group shutdown, `lost`
means uncertainty, and a nonterminal status with `cancellation_requested` means
the request is still pending. Cancellation is not rollback or remote termination;
see [Background jobs](guides/jobs.md#startup-and-cancellation) for bounds and recovery.

For Codex, configure absolute paths:

```toml
[mcp_servers.ridge]
command = "/path/to/environment/bin/ridge-mcp"
args = ["--config", "/path/to/ridge.yaml"]
required = true
default_tools_approval_mode = "writes"
```

The server provides explicit tools for discovery, inspection, execution,
data operations (`list_data`, `read_data`, `write_data`, `stat_data`), copy,
and job lifecycle. `list_resources` returns
concise capability summaries; call `inspect_resource` only for backend
properties. Both results distinguish operations supported by a resource from
operations allowed by the configured Ridge policy. They also report canonical
background-capable operations when durable jobs are available, plus `provider`,
`addressing`, and `supports_copy`. Copy remains a separate application workflow.

Data tools use `path` for either a relative filesystem path or an exact object
key/prefix. `list_data` accepts `cursor` and `limit` and returns `addressing`,
`entries`, and `next_cursor`. `stat_data` returns `addressing` and `metadata` with
filesystem or object-specific fields. See [data semantics](concepts/resources.md).

Data and execution results are bounded:

- filesystem and storage lists are paginated;
- only UTF-8 reads of at most 64 KiB are inlined;
- binary and larger content returns a descriptor and should be moved with
  `copy`;
- stdout and stderr are independently limited to 32 KiB while retaining their
  full byte counts.

Resource discovery grows with the configured inventory. `list_jobs` currently
returns all authorized full job records without pagination; do not assume every
MCP response has a fixed bound. Use `inspect_job` for a known ID and bounded
`read_job_logs` pages to reconnect. See [job limitations](guides/jobs.md#current-limitations).

The execution, write, and copy tools accept `background=true` and return a
response whose `mode` is `completed` or `submitted`. Submitted responses contain
a job handle. Use `list_jobs`, `inspect_job`, `read_job_logs`, and `cancel_job`
to reconnect. Use background mode when runtime is uncertain, cancellation or
incremental local logs matter, or a synchronous tool timeout is likely. Provide
an `idempotency_key` before retrying a submission.

Execution has no timeout by default in either mode. Set `timeout_seconds` to a
finite value when the attempt must be bounded.

Because MCP inline reads first determine whether content fits in model context,
`read_data` requires both `data.stat` and `data.read`. CLI reads do not
perform that preliminary metadata operation.

MCP annotations describe likely side effects. They are host hints, not Ridge
authorization. MCP operations are authorized by the same application service as
CLI operations. The server still inherits the same operating-system authority
and ambient credentials as the CLI, and Ridge policy does not constrain direct
access outside Ridge.

The Codex host can reject an MCP tool before Ridge sees it. This is useful
defense in depth, but it is not evidence that Ridge policy denied the request.
When testing Ridge authorization itself, set the server's tool approval mode so
the request reaches Ridge and verify Ridge's authorization error and downstream
side effects separately.
