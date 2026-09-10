# MCP server

`ridge-mcp` exposes a [workspace's](configuration.md#workspace) configured resources
over local stdio using the official MCP Python SDK. Point participating clients
at the same configuration to share policy and managed state. It has no network listener.

For delegated tasks, start a separate connection/process with `RIDGE_SCOPE_TOKEN`
or `--scope-token-file PATH`. The handle is captured at startup, while authority is
rechecked for each call. Never pass access handles as individual tool arguments.
`inspect_access`, `create_scope`, `list_scopes`, `inspect_scope`, and `revoke_scope`
share the [task delegation contract](guides/delegation.md)
with CLI/Python. `create_scope` accepts a list of structured resource grants and
optional absolute `expires_at`; only its result includes the new bearer token.
Grant `data_root` narrows data addressing relative to the parent view;
`inspect_access` reports the inherited `data_root_chain`. See
[rooted data semantics](guides/delegation.md#narrow-data-views) before using
compute alongside a narrower data grant.

See [client setup](integrations/clients.md) for executable/configuration recipes,
desktop connections and child bindings. The [agent-led handoff](integrations/agents.md)
demonstrates a programmable host using process-local client configuration without
editing personal settings. Client configuration belongs in those guides; the
tool contracts below apply to every MCP client.

The server provides explicit tools for discovery, inspection, execution,
data operations (`list_data`, `read_data`, `write_data`, `stat_data`, `delete_data`), copy,
job lifecycle, and resource coordination. `list_resources` returns
concise capability summaries; call `inspect_resource` only for backend
properties. Both results distinguish operations supported by a resource from
operations allowed by the configured Ridge policy. They also report canonical
background-capable operations when durable jobs are available, plus `provider`,
`addressing`, and `supports_copy`. Copy remains a separate application workflow.

Start with [delegating work](guides/delegation.md): the parent agent derives
access, the host binds child connections, and each child uses these ordinary tools.
The [sales-report example](examples/csv-report.md#with-an-agent) demonstrates the
copy/run/retrieve portion; [integrations](integrations.md) covers connection choices.

Data tools use `path` for either a relative filesystem path or an exact object
key/prefix. `list_data` accepts `cursor` and `limit` and returns `addressing`,
`entries`, and `next_cursor`. `stat_data` returns `addressing` and `metadata` with
filesystem or object-specific fields. See [data semantics](concepts/resources.md).

`delete_data(resource, path, recursive=false)` deletes one exact path/key using
only `data.delete`. Its completed envelope contains `result: {outcome: ...}`
(`deleted`, `missing`, or S3 `acknowledged`) and `job: null`. Nonempty directories
require explicit recursion; S3 rejects recursion. Deletion is marked destructive
and conservatively non-idempotent because versioned S3 can create new delete
markers on repeated calls. See [deletion](concepts/resources.md#deletion).

## Content and discovery bounds

MCP limits inline payloads and execution output:

- filesystem and storage lists are paginated;
- only UTF-8 reads of at most 64 KiB are inlined;
- binary and larger content returns a descriptor and should be moved with
  `copy`;
- stdout and stderr are independently limited to 32 KiB while retaining their
  full byte counts.

These are presentation limits, not a bound on execution memory. Foreground
execution captures output before formatting it; Docker/SSH helpers also buffer
command output. Background local execution writes directly to durable logs.
Resource properties, names, and full job inspection do not have a universal
response-byte cap.

Read size metadata is a preflight check, not a snapshot. If content grows past
the limit during a built-in read, the tool reports a size-limit error; use `copy`.
MCP independently checks returned bytes before decoding or inlining them, even
if a provider does not enforce the requested read limit.

Ridge formats primary failures and secondary recovery notes into at most 16 KiB
of UTF-8 diagnostic text, before frontend/protocol prefixes, with explicit truncation. See
[copy recovery](guides/copying.md) for retained staging and unconfirmed publication.

Resource discovery grows with the configured inventory. `list_jobs(limit=50,
cursor=None)` returns authorized summaries in a `jobs`/`next_cursor` page,
newest first; limits are 1–200. Summaries omit results, errors, and cancellation
intent. Use `inspect_job` for those details and bounded `read_job_logs` pages to
reconnect. See [job discovery](guides/jobs.md#discovering-jobs) for cursor and
changing-history semantics.

## Background work

The execution, write, delete, and copy tools accept `background=true` and return a
response whose `mode` is `completed` or `submitted`. Submitted responses contain
a job handle. Use `list_jobs`, `inspect_job`, `read_job_logs`, and `cancel_job`
to reconnect. Use background mode when runtime is uncertain, cancellation or
incremental local logs matter, or a synchronous tool timeout is likely. Provide
an `idempotency_key` before retrying a submission.

Execution has no timeout by default in either mode. Set `timeout_seconds` to a
finite value when the attempt must be bounded.

Background cancellation records durable intent and survives a client disconnect.
Inspect the returned job: `cancelled` confirms owned-local-group shutdown, `lost`
means uncertainty, and a nonterminal status with `cancellation_requested` means
the request is still pending. Cancellation is not rollback or remote termination;
see [Background jobs](guides/jobs.md#startup-and-cancellation) for bounds and recovery.

## Resource sessions

Because MCP inline reads first determine whether content fits in model context,
`read_data` requires both `data.stat` and `data.read`. CLI reads do not
perform that preliminary metadata operation.

Resource operations automatically acquire claims. To reserve resources across
calls, `acquire_locks` accepts `scopes: [{resource, operation, path?}, ...]`, optional
`lease_seconds` (default 300), and `wait_seconds` (default 0). Pass the returned
`token` as `lock_token` on execution, data, and copy calls; session reads must
declare both `data.stat` and `data.read`. Use `renew_locks` and `release_locks`
with `token`. `inspect_lock(identity)` and `list_locks(cursor, limit)` expose
authorized metadata without ownership tokens. `force_release_lock(identity,
reason)` is only for uncertain operations and never cancels them. Sessions survive
MCP disconnection and can also be used from the CLI. See
[coordination and recovery](guides/coordination.md).

Lock metadata's `claims` is a list of `{domain, scope, mode}` values; `scope` is
an opaque component array or `null` for the whole domain. A reservation's optional
`path` names a filesystem file/tree or exact S3 key. Omit it to reserve the whole
resource. Different files in the same directory can overlap when their complete
effects are covered; unsupported narrow reservations fail rather than expanding.

Hosts that own a multi-call workflow can use the Python `ManagedMCPSession`
caller helper for automatic renewal and token injection. This requires host
integration; the server does not keep idle sessions alive on its own. See
[managed caller sessions](guides/coordination.md#managed-caller-sessions) for a
runnable example, cancellation, and failure behavior.

## Host approvals and Ridge policy

MCP annotations describe likely side effects. They are host hints, not Ridge
authorization. MCP operations are authorized by the same application service as
CLI operations. A host can reject a call before Ridge receives it; see
[Authorization](concepts/authorization.md) for these independent gates and
[Development](development.md) for testing them separately.
