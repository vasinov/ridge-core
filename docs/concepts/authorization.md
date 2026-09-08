# Authorization

One trusted operator selects the policy used by cooperating callers. Exact grants
control which resource operations those callers can perform through Ridge.
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
