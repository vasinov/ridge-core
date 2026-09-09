# Security model

Ridge supports one trusted operator with multiple cooperating agents in a
[workspace](configuration.md#workspace). Its optional
exact permission policy controls resource operations through the application
service shared by CLI and MCP. It is not a sandbox: process authority and direct
access remain governed by the operating system and downstream services.

The operating model is one authoritative configuration and shared managed state,
possibly used by several CLI/MCP processes. Coordination and policy apply to those
participating requests, not unrelated configurations or external interventions.

## Ambient authority

The Ridge process inherits its operating-system identity, environment, files,
network access, OpenSSH configuration and agents, Docker access, and cloud
credentials. Resource configuration narrows the paths or prefixes presented by
Ridge; it does not remove authority already available to the process.

Local commands execute with the Ridge user's authority. Docker access commonly
implies broad control over the Docker daemon. SSH operations use the configured
remote account. S3 uses Boto3's ambient credential chain.

## Filesystem boundaries

Built-in filesystem operations reject absolute paths and paths that resolve
outside their effective root, including symbolic-link escapes. Delegated data
views validate each inherited root boundary when used; compute is not narrowed.
Contained paths
such as `nested/../file` are accepted. Tree copy rejects
absolute, broken, escaping, and top-level links plus hard links and special
files. Deletion instead resolves parents and unlinks the final symbolic link,
even if its target is missing or outside the root; recursion never follows links.
It rejects deletion of the resource root. See [deletion](concepts/resources.md#deletion).

Path validation is not containment against hostile concurrent filesystem changes.
Use roots whose directory structure is controlled by trusted participants, and
use native isolation when running untrusted code. These checks cannot restrict an
arbitrary command executed on the same resource unless a native operating
system, account, or container boundary also applies.

## Providers

Installed providers are trusted Python code loaded into the Ridge process.
They can perform actions during import, construction, inspection, or capability
execution and are not contained by the resource path boundary.

## Durable state

Task access handles are bearer credentials, distinct from lock ownership tokens.
Ridge stores their hashes, but a host receiving `create_scope` can retain the
returned handle in its transcript. Pass handles through protected token files or
process environment, not prompts or per-tool arguments. Invalid binding fails
closed. Scope revocation closes future access; cancelling running work remains
separate. See [delegation](concepts/authorization.md#create-bind-and-close-a-task).

Ridge removes ambient `RIDGE_SCOPE_TOKEN` from local compute and job-supervisor
environments. Explicit command environment values are still caller data; this is
not general credential redaction or isolation from the host.

Job arguments, staged write content, output, and errors may
contain sensitive data. Metadata and logs have no automatic expiration. Ignore
`.ridge/` (and any custom state directory) in your own repository and review
artifacts before publishing. See [retention](guides/jobs.md#retention-and-sensitive-data).
Place state outside resource trees that callers can delete or replace; coordination
does not protect its own database from a resource operation targeting those files.

Coordination sessions and operations share the jobs database. Session tokens are
returned to callers and stored as hashes; protect the returned token and any shell
environment containing it. A token proves cooperative ownership, not authorization.
Calls still require current grants. Coordination covers participating Ridge calls
with declared resource scopes; different state directories, unrecognized aliases,
arbitrary compute access, and external tools can bypass it. Force-release records
an operator reason and does not terminate work. See [coordination](guides/coordination.md).

For cancellation, distinguish a recorded request from verified `cancelled` status.
Verification covers the owned local worker group, not remote or detached processes,
and does not roll back effects. Follow [job recovery](guides/jobs.md#current-limitations)
before retrying lost work or deleting its state.

## MCP

The MCP server communicates over local stdio and opens no network listener.
Tool annotations help the host classify side effects but do not authorize
requests. Ridge's own permission checks run in the application layer shared by
MCP and CLI. Configure host approval and sandboxing independently.

See [Authorization](concepts/authorization.md) for exact policy semantics,
enforcement guarantees, and known bypasses.
