# Authorization

One trusted operator selects the workspace policy used by cooperating callers.
Exact grants control which resource operations those callers can perform through Ridge.
The [security model](../security.md) covers ambient authority and native isolation.

Ridge separates three decisions:

```text
effective operation
    = supported by the resource
    ∩ allowed by Ridge policy
    ∩ permitted by downstream authority
```

Policy semantics:

- one operator-selected workspace policy, optionally narrowed by a task scope;
- unrestricted trusted mode when no policy exists;
- default-deny, exact resource-and-operation grants when a policy exists;
- no path globs, conditions, ownership, inheritance, or explicit deny rules;
- identical enforcement through CLI and MCP;
- source `data.read` and destination `data.write` authorization before
  either side of a copy is opened.

In operator mode, resource discovery and inspection remain outside policy. They report every
configured resource, its supported operations, its policy-allowed operations,
and provider inspection properties. Clients able to reach a Ridge frontend can
therefore see this configuration metadata.

The exact grant vocabulary is `compute.exec`, `data.list`, `data.read`,
`data.write`, `data.stat`, and `data.delete`. There are no separate copy/transfer grants.
`data.write` allows whole-tree replacement by copy on filesystem resources,
including removal of destination-only entries. For files-only local access,
grant the needed data operations and omit `compute.exec`. Explicit deletion needs
`data.delete`; withholding it does not prevent removal through whole-tree
replacement or arbitrary execution. Recursion is an operation parameter, not a grant.

CLI and local stdio bind to operator authority unless started with a task handle.
A handle identifies task authority, not a permanent user or agent identity.

An MCP host's tool approval is another independent gate before Ridge. If the
host refuses a tool call, Ridge never receives an authorization request. Testing
Ridge denial therefore requires the host to permit the call to reach Ridge; that
does not make the operation allowed by Ridge policy.

Ridge guarantees that application operations check policy before invoking their
target capability, CLI and MCP share that application boundary, and copy checks
both endpoints before opening either. It does not claim that a Ridge policy is
enforced outside Ridge. Installed providers are trusted in-process and can
bypass application mediation internally.

Arbitrary `compute.exec` is especially powerful. Filesystem restrictions on
the same resource are not a security boundary once execution can bypass them,
unless the operating system, account, container, or another native mechanism
enforces the restriction.

## Delegated task access design

CLI, MCP, and Python support durable task scopes over named resources, optionally
with provider-validated narrower data roots. Unsupported views are rejected, never
silently treated as whole-resource access. Lock tokens establish reservation
ownership, not delegation.

### Create, bind, and close a task

The operator explicitly enables [delegation](../configuration.md#delegation-policy):

```yaml
permissions:
  inputs: [data.read, data.stat]
delegation:
  inputs: [data.read, data.stat]
```

Create a read-only task, with no further delegation:

```bash
ridge --config ridge.yaml scope create \
  --grant '{"resource":"inputs","operations":["data.read","data.stat"]}'
```

The JSON result contains `scope` metadata and a secret `token`, returned only
once. Pass that token to the child process as `RIDGE_SCOPE_TOKEN`, or place only
the token in a protected file and launch:

```bash
ridge --config ridge.yaml --scope-token-file /private/task.token access inspect
ridge-mcp --config ridge.yaml --scope-token-file /private/task.token
```

An explicit token file overrides the environment. Its content is read once at
startup; replacing the file does not rebind a running MCP connection. Empty,
invalid, expired, revoked, and wrong-workspace tokens fail closed. They never
select operator mode. Do not put handles in prompts, committed files, or per-tool
arguments. MCP creation returns the handle to its host, so protect host transcripts.

`access inspect` reports effective use and delegation grants. `scope list`,
`scope inspect ID`, and `scope revoke ID` inspect or close visible task scopes.
Creation accepts repeated `--grant` JSON objects, optional `--expires-at` with an
absolute timezone-aware timestamp, and an explicit `delegation` operation list
inside each grant when further derivation is needed. Omitting child expiry
inherits its parent's bound. Each resource appears at most once per scope.
The equivalent MCP tools are `inspect_access`, `create_scope`, `list_scopes`,
`inspect_scope`, and `revoke_scope`.

Clients can reconnect with the same handle while the task remains active.
Close it explicitly on task completion. A parent can revoke a descendant without
its bearer handle. Revocation blocks new work and result access, but does not
cancel admitted jobs or release outstanding resource reservations.

Scoped discovery exposes only granted resource names. Scope and job history is
subtree-only; siblings and operator-owned jobs remain hidden. Job observation
requires current use grants for one's own jobs. For descendant jobs, current use
or delegation grants authorize inspection, logs/results, and cancellation; a
delegate-only parent does not gain direct resource use. Idempotency keys are local to
the submitting scope, so two children can use the same key independently.
Scoped lock tokens can be used or renewed only by their owning scope. Parents
can inspect descendant claims; operator recovery remains available. `config validate`
is an operator workflow; scoped callers use `access inspect` instead.

### Narrow data views

Add `"data_root":"outputs/task-a"` to a resource grant to address only that data
view. It is relative to the issuing parent's view; omission inherits that view,
not the original resource root. `access inspect` reports both the issued
`data_root` and effective `data_root_chain`. Names, state, and canonical locks
remain unchanged. A grant's data view applies to both use and further delegation.

Local, Docker, and SSH validate relative root syntax at creation (no absolute
paths or `..` components). On every data operation, each inherited directory must
exist, be a directory, and resolve within its parent view, including symlinks.
These checks run under the whole-resource operation claim; creation never contacts
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

### Accepted direction

A task receives an access scope derived from existing workspace resources.
Child access is bounded by the parent's delegable authority and the operator's
policy. Using an operation and delegating it are separate permissions. Resource
registration and broader authority remain operator configuration decisions.

Scopes are **task-lived, explicitly revocable, and optionally expiring**. They are
not tied to agent connections: disconnecting or changing the client does not end
the task's access. A host can resume a task and inspect its background work.
The agent harness owns spawning, routing, and task completion; Ridge owns scope
derivation and checks on participating requests within the workspace.

An access scope grants no lock. A lock session reserves resources for a workflow;
an operation's lock footprint describes the effects it must coordinate. Scope
expiry/revocation closes access admission, not proof that already-admitted work
has stopped. Existing cancellation and conservative claim recovery remain separate.

Resource views retain canonical coordination identity. For example, a view rooted
at a parent's `outputs` directory and the parent location `outputs/report.csv`
must identify the same target when the child addresses `report.csv`. Filesystems
use the parent's whole-resource lock domain; S3 exact objects use canonical full-key
footprints when the domain's aliases are compatible.
Neither child names nor task IDs create independent locks or state directories.

### Accepted first implementation

The following choices define the implemented scope contract.

- **Authority and binding:** persist scope records in the workspace's local state;
  bind each CLI/MCP client to one opaque scope handle at startup. Invalid, expired,
  or revoked handles fail closed rather than falling back to operator access.
  Keep one coordination host and existing transports initially, without a daemon
  or hosted team service. Return bearer handles once, persist only their hashes,
  and pass them through `RIDGE_SCOPE_TOKEN` or a startup token-file option, never
  individual tool arguments. Bind workspace identity to the resolved configuration
  path and state directory; moving either requires fresh delegation.
- **Derivation:** start with subsets of named resources and exact operations,
  plus rooted data views where the provider can validate narrowing. Keep arbitrary
  compute resource-wide. No redelegation by default; when permitted,
  every descendant must remain within its ancestors' current ceilings. Unsupported
  view types fail explicitly rather than silently broadening access.
  Root issuance is nonconnecting: validate existence and physical containment
  when the data view is used, under the operation's canonical resource lock.
  Creating a scope does not create its narrowed directory.
- **Lifetime:** optional absolute expiry, no connection heartbeat for permission
  lifetime. Explicit task completion revokes its scope. Ancestor revocation
  or expiry disables descendant admission too; reconnect never revives closed access.
  Closed scopes lose result access too; an authorized parent retains
  access to task history. This is distinct from the renewable lock-session idle lease.
- **Jobs and visibility:** attach scope lineage to submissions and filter resource
  discovery, jobs, logs, and diagnostics by the bound scope. Parents can
  inspect/cancel descendant work, children see only their own subtree, and siblings
  do not see each other's metadata. Revocation blocks new work; admitted jobs keep
  their recorded outcome/claims and require separate cancellation.
- **Configuration changes:** comments, formatting, mapping order, and unrelated
  resource edits do not invalidate scopes. Current policy can reduce their effective
  authority but cannot expand their issued grants; wider access requires fresh
  delegation. Changing a granted resource's provider configuration, provider name,
  or coordination identity invalidates affected scopes and descendants. Treat all
  provider-option edits conservatively as identity changes rather than guessing
  semantic equivalence. Scope identity includes its workspace, not just a resource
  name. Background jobs use the same resource-level semantic checks before execution;
  already-running operations retain their admitted configuration.

The approved frontend shape is `scope create/list/inspect/revoke` and
`access inspect`, with equivalent MCP tools. Creation takes structured resource
grants, optional narrower data roots, expiry, and explicit redelegation grants.
Keep resource names unchanged, with one view per resource per scope. The
[`delegation` map](../configuration.md#delegation-policy) is an explicit operator
ceiling, separate from ordinary permissions; omission disables delegation.

The existing `ridge-setup` skill covers access inspection, derivation, child
binding, reconnect, and task closure. Prefer deriving
access in an existing workspace over editing operator configuration. Ridge owns
enforcement, docs own the complete contract, and harness integration instructions
own spawning and connection setup. Plugin packaging reuses the skill and installed
MCP server; it does not implement authorization.

The reference scenario is a persistent Ridge host connecting a caller to a remote
worker and separate input/result resources. A client can disconnect while an
experiment runs and reconnect to inspect it; host survival and remote process
cancellation follow the jobs contract. Phone UI and GPU/provider integration are
separate acceptance work, not prerequisites for this local scope design.
