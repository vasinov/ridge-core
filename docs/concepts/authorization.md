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

- one operator-selected policy per Ridge process, without users or roles;
- unrestricted trusted mode when no policy exists;
- default-deny, exact resource-and-operation grants when a policy exists;
- no path globs, conditions, ownership, inheritance, or explicit deny rules;
- identical enforcement through CLI and MCP;
- source `data.read` and destination `data.write` authorization before
  either side of a copy is opened.

Resource discovery and inspection remain outside policy. They report every
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

CLI and local stdio do not establish independent caller identities; grants apply
to the process's selected policy, not to individual agents.

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

This section describes planned behavior, not an available API. Current releases
use the process-wide policy above; there are no access-scope creation commands or
access tokens. Existing lock tokens establish reservation ownership, not delegation.

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
must identify the same target when the child addresses `report.csv`. Until granular
coordination is implemented, both use the parent's whole-resource lock domain.
Neither child names nor task IDs create independent locks or state directories.

### Proposed first implementation — awaiting approval

The following choices need approval before runtime implementation:

- **Authority and binding:** persist scope records in the workspace's local state;
  bind each CLI/MCP client to one opaque scope handle at startup. Invalid, expired,
  or revoked handles fail closed rather than falling back to operator access.
  Keep one coordination host and existing transports initially, without a daemon
  or hosted team service. Settle handle transport/storage and workspace identity
  before defining the wire API.
- **Derivation:** start with subsets of named resources and exact operations,
  plus rooted data views where the provider can validate narrowing. Keep arbitrary
  compute resource-wide. Recommend no redelegation by default; when permitted,
  every descendant must remain within its ancestors' current ceilings. Unsupported
  view types fail explicitly rather than silently broadening access.
- **Lifetime:** optional absolute expiry, no connection heartbeat for permission
  lifetime. Explicit task completion revokes its scope. Recommend ancestor revocation
  or expiry disables descendant admission too; reconnect never revives closed access.
  Recommend closed scopes lose result access too; an authorized parent retains
  access to task history. This is distinct from the renewable lock-session idle lease.
- **Jobs and visibility:** attach scope lineage to submissions and filter resource
  discovery, jobs, logs, and diagnostics by the bound scope. Recommend parents can
  inspect/cancel descendant work, children see only their own subtree, and siblings
  do not see each other's metadata. Revocation blocks new work; admitted jobs keep
  their recorded outcome/claims and require separate cancellation. Define handling
  of policy/config changes between submission and worker execution before coding.

The reference scenario is a persistent Ridge host connecting a caller to a remote
worker and separate input/result resources. A client can disconnect while an
experiment runs and reconnect to inspect it; host survival and remote process
cancellation follow the jobs contract. Phone UI and GPU/provider integration are
separate acceptance work, not prerequisites for this local scope design.
