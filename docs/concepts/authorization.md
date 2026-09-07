# Authorization

Ridge runs as a trusted, single-user process. Its optional permission policy can
attenuate what an agent does through Ridge, but it is not authentication, a
sandbox, or a replacement for downstream access control.

Ridge separates three decisions:

```text
effective operation
    = supported by the resource
    ∩ allowed by Ridge policy
    ∩ permitted by downstream authority
```

The initial policy is intentionally narrow:

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
`data.write`, and `data.stat`. There are no separate copy/transfer grants.
`data.write` allows whole-tree replacement by copy on filesystem resources,
including removal of destination-only entries. For files-only local access,
grant the needed data operations and omit `compute.exec`.

Authentication and authorization are distinct. A local stdio process does not
establish an independently authenticated caller, so user identities, tokens,
and hosted-service authentication remain outside this experiment.

An MCP host's tool approval is another independent gate before Ridge. If the
host refuses a tool call, Ridge never receives an authorization request. Testing
Ridge denial therefore requires the host to permit the call to reach Ridge; that
does not make the operation allowed by Ridge policy.

Ridge describes enforcement with separately scoped facts:

- **Ridge policy decision:** whether the exact resource operation is allowed;
- **boundary coverage:** whether every Ridge-owned route to that operation
  performs the same check;
- **known bypasses:** whether another allowed Ridge operation can produce the
  restricted effect;
- **downstream authority:** whether an independent operating-system or service
  boundary permits the action.

Ridge guarantees that application operations check policy before invoking their
target capability, CLI and MCP share that application boundary, and copy checks
both endpoints before opening either. It does not claim that a Ridge policy is
enforced outside Ridge. Installed providers are trusted in-process and can
bypass application mediation internally.

Arbitrary `compute.exec` is especially powerful. Filesystem restrictions on
the same resource are not a security boundary once execution can bypass them,
unless the operating system, account, container, or another native mechanism
enforces the restriction.
