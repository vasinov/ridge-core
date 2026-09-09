# Authorization

The operator establishes a workspace's initial authority. Agents derive tailored
task access for their subagents within that ceiling. The
[delegation guide](../guides/delegation.md) covers creation, binding, supervision,
reconnect, and closure.

## Support, use, and delegation

A resource's supported operations describe what it implements. Workspace
`permissions` control what callers may use; `delegation` controls what they may
pass to children. Downstream OS and service authority must also permit the work.

```text
child use or delegation
    ⊆ parent's effective delegation
    ⊆ workspace permissions ∩ workspace delegation
```

Issued use and delegation sets are independent. A parent can supervise a task
through delegation authority without having direct use access itself. Children
receive no further delegation unless explicitly granted.

Omitting `permissions` selects unrestricted trusted mode. When present, the map
is exact and default-deny; `permissions: {}` denies all resource operations.
Omitting `delegation` always disables delegation, even in unrestricted use mode.
See [configuration](../configuration.md#permissions) for the schema.

Policy matches exact resource and operation names, not path globs, conditions,
or explicit deny rules. Parent-relative `data_root` views narrow a task's data
addressing separately; their inheritance is described in
[narrow data views](../guides/delegation.md#narrow-data-views).

## Operation grants

The grant vocabulary is `compute.exec`, `data.list`, `data.read`,
`data.write`, `data.stat`, and `data.delete`. CLI and MCP share enforcement.

- Copy needs source `data.read` and destination `data.write`, checked before
  either endpoint opens. There is no separate copy grant.
- MCP inline reads additionally require `data.stat`; CLI reads do not.
- `data.write` includes whole-tree replacement by filesystem copy, removing
  destination-only entries.
- Explicit deletion needs `data.delete`. Recursion is a request parameter,
  not another grant.
- `compute.exec` permits arbitrary execution with the resource's ambient authority.
  Data roots do not narrow it. Withholding deletion does not prevent removal
  through arbitrary execution or whole-tree replacement.

Use data-only grants for tasks that should operate only through rooted data
interfaces. The [security model](../security.md) owns native isolation and
trusted-provider boundaries.

## Binding and visibility

CLI and local MCP connections use operator authority unless bound to a task
handle at startup. Handles identify task authority, not permanent agent identities.
Invalid bindings fail closed.

Operator discovery reports every configured resource, supported and allowed
operations, and provider properties. Scoped discovery shows only granted resource
names. Scope and job history is limited to the caller's subtree; siblings and
operator-owned jobs are hidden.

Own job observation requires current use grants. Descendant job inspection,
logs/results, and cancellation accept current use or delegation grants for every
underlying operation. This does not authorize direct reads of descendant artifacts;
retrieving resource data still needs the corresponding use grant.

## Authority is not coordination

An access scope grants permissions; a lock session reserves resources; an action
footprint describes interference. Creating access does not acquire locks, and a
lock token does not grant permission. Scoped views preserve canonical resource
identity and share workspace coordination.

Task expiry/revocation closes future access, not already-admitted work.
Cancellation and reservation release are separate actions. See
[delegation lifecycle](../guides/delegation.md#finish-or-stop-a-task).

## Host approvals

An MCP host's tool approval is an independent gate before Ridge. If the host
refuses a call, Ridge receives no request. A host permitting a tool call does not
make it authorized by Ridge.

Application operations check policy before invoking target capabilities. Tests
of Ridge denial must let the call reach Ridge, then inspect the denied outcome
and absence of target effects.
