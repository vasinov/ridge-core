# Delegating work

An agent can turn a broad task into smaller tasks with tailored resource access.
It chooses each child's resources, permitted operations, and data roots, then
derives that access through Ridge. The operator establishes the workspace's
initial ceiling; no per-child configuration edit is needed.

A task is an assignment whose completion the agent or harness judges. An access
scope records delegated authority, resource views, identity, lineage, and access
lifecycle. One scope per assignment is a useful convention; a scope can serve
multiple connections, and disconnecting does not close it.

Ridge owns scope derivation and enforcement. The agent harness owns spawning,
prompts, dispatch, and connection setup. Start with the
[runnable handoff example](../examples/delegation.md), or follow the workflow below
with an [MCP-connected agent](../integrations.md).

## Discover and divide the work

Call `inspect_access` to inspect effective use and delegation grants, then
`list_resources` for available capabilities. An agent can delegate only within
its current delegation grants. A missing grant is a workspace policy decision,
not a reason to retry using operator authority.

For two independent evaluations, share read access to the project and dataset,
assign a distinct existing worker to each child, and narrow each child's results
view to its own location. Keep resource names and workspace state unchanged.

An access scope does not reserve resources. Independent data paths can overlap,
including files in the same directory. Compute still reserves the whole resource;
use distinct workers for parallel compute, or sequence work on a shared worker. See
[coordination](coordination.md) for multi-step reservations.

## Create task access

The workspace must explicitly enable the needed operations in its
[delegation policy](../configuration.md#delegation-policy). For example, an agent
can derive a read-only child without allowing further delegation:

```python
# Ridge MCP tool call:
create_scope(
    grants=[
        {"resource": "inputs", "operations": ["data.read", "data.stat"]},
    ]
)
```

The equivalent CLI call is:

```bash
ridge --config ridge.yaml scope create \
  --grant '{"resource":"inputs","operations":["data.read","data.stat"]}'
```

The result contains `scope` metadata and a secret `token`, returned only once.
Record the scope ID for supervision and closure; deliver the token through the
host's child-launch mechanism.

To allow a child to delegate further, include a `delegation` list in its grant.
Use and delegation are separate: a child can delegate an operation without being
allowed to perform it directly. Both lists must fit the parent's delegable
authority. Every resource appears at most once in a scope.

Creation accepts optional `expires_at` (CLI `--expires-at`) as an absolute
timezone-aware timestamp. Omission inherits the parent's expiry bound. A child
cannot outlive that bound.

## Bind and verify the child

Launch a separate child CLI/MCP process with `RIDGE_SCOPE_TOKEN` in its
environment, or store only the token in a protected file and launch:

```bash
ridge --config ridge.yaml --scope-token-file /private/task.token access inspect
ridge-mcp --config ridge.yaml --scope-token-file /private/task.token
```

The child calls `inspect_access` and verifies its expected scope ID and effective
grants before working. Naming a child in a prompt does not rebind an existing
operator connection. See [integrations](../integrations.md) for host setup.

An explicit token file overrides the environment and is read once at startup.
Replacing the file does not rebind a running connection. Empty, invalid, expired,
revoked, and wrong-workspace tokens fail closed; they never select operator mode.

Do not put bearer tokens in child prompts, committed files, or per-tool arguments.
MCP creation returns the token to its host, so protect host transcripts too.
Python hosts use `RidgeService.from_config(..., scope_token=token)` explicitly;
that API does not read environment binding.

## Narrow data views

Add `"data_root":"outputs/task-a"` to a resource grant to address only that data
view. It is relative to the issuing parent's view; omission inherits that view,
not the original resource root. `access inspect` reports both the issued
`data_root` and effective `data_root_chain`. Names, state, and canonical locks
remain unchanged. A grant's data view applies to both use and further delegation.

Local, Docker, and SSH validate relative root syntax at creation (no absolute
paths or `..` components). On every data operation, each inherited directory must
exist, be a directory, and resolve within its parent view, including symlinks.
These checks run under the admitted operation claims; creation never contacts
the remote backend or creates a directory. Missing roots can be prepared separately
by an authorized parent. Data paths and returned listings are relative to the final
view. Copy and background data jobs use that same view; deletion cannot remove its root.

For S3, `data_root` is a nonempty relative prefix without leading/trailing `/` or
NUL. It appends to the parent's prefix with a `/` separator, matching configured
S3 prefix semantics. Interior slashes and `..` remain literal key text, not filesystem
navigation. No directory-existence probe applies to object prefixes.

`compute.exec` remains resource-wide, including its working directory; a data root
does not narrow arbitrary execution. Omit compute grants for data-only tasks.
Custom providers must explicitly support data views; otherwise narrowing fails.


## Work, supervise, and reconnect

Children use ordinary Ridge operations against their scoped resource views.
Copies and background jobs retain those views. A child sees only its granted
resource names; scope and job history is subtree-only, hiding siblings and
operator-owned jobs.

The parent can inspect descendant jobs, read their logs/results, and request
cancellation within its current use **or delegation** grants. A delegate-only
parent does not gain direct resource use. A caller's own jobs still require use
grants. Job idempotency keys are local to the submitting scope.

Reconnect with the same handle while the access scope remains active; connection loss
does not close access. Do not create replacement scopes merely because a client
disconnected. Background jobs remain on the Ridge host: reconnect is not a
promise that work survives host shutdown. See [jobs](jobs.md).

If a child needs a multi-step reservation, it acquires and renews its own session.
Only the owning scope can use or renew its lock token. A parent must not hold a
conflicting reservation while waiting for a child that needs the same resource.
Parents can inspect descendant claims; recovery follows the
[coordination contract](coordination.md).

## Finish or stop a task

Inspect job results and command exit codes, and retrieve needed artifacts. Then
close access with `revoke_scope(identity=scope_id)` or `ridge scope revoke ID`.
The parent needs the scope ID, not the child's bearer token.

Revocation or expiry blocks new operations and result access for the scope and
its descendants. It does **not** cancel admitted jobs or release reservations.
When stopping early, revoke access to block new admission, request authorized job
cancellation, and inspect jobs and reservations for unsettled work. Only the owning
scope can use its session token; parent inspection does not grant that ownership.
Follow [reservation recovery](coordination.md) for outstanding claims.
Authorized parents retain access to descendant job history after its scope closes.

Use `list_scopes` / `scope list` and `inspect_scope` / `scope inspect ID` for
visible scope metadata. These never return bearer handles.
`config validate` is an operator setup workflow; bound agents use
`inspect_access` / `access inspect`.

## When configuration changes

Comments, formatting, mapping order, and unrelated resource edits do not invalidate
access scopes. Current policy can reduce effective access; restoring policy can
restore an issued grant but cannot add one. Broader task access needs fresh issuance.

Changing a granted resource's provider configuration, provider name, or lock
identity invalidates affected scopes and descendants. All provider-option edits
count, including descriptive properties; Ridge does not guess semantic equivalence.
An ancestor's identity checks include resources omitted by its children.

Scope identity includes the resolved configuration path and state directory.
Moving either requires fresh delegation. Once an identity mismatch or expiry is
observed and persisted as closure, restoring the old configuration does not revive it.
Already-admitted work follows the separate [job configuration contract](jobs.md#status-and-configuration).

The [authorization concept](../concepts/authorization.md) owns permission rules;
[architecture](../architecture.md#authorization) owns persistence and admission
invariants. The existing setup skill assists access derivation; plugins and
harness adapters reuse Ridge's enforcement rather than implement their own.
