# Security model

Ridge is currently intended for controlled, single-user environments. It is
not a sandbox, credential broker, or complete authorization boundary.

An optional exact permission policy can reduce which operations clients perform
through Ridge. It does not reduce the process's ambient authority or prevent
direct access outside Ridge.

## Ambient authority

The Ridge process inherits its operating-system identity, environment, files,
network access, OpenSSH configuration and agents, Docker access, and cloud
credentials. Resource configuration narrows the paths or prefixes presented by
Ridge; it does not remove authority already available to the process.

Local commands execute with the Ridge user's authority. Docker access commonly
implies broad control over the Docker daemon. SSH operations use the configured
remote account. S3 uses Boto3's ambient credential chain.

## Filesystem boundaries

Built-in filesystem operations reject absolute paths, `..` traversal, and
symbolic-link escapes from their configured root. Safe tree copy rejects
absolute, broken, escaping, and top-level links plus hard links and special
files.

These checks mediate Ridge filesystem operations. They cannot restrict an
arbitrary command executed on the same resource unless a native operating
system, account, or container boundary also applies.

## Providers

Installed providers are trusted Python code loaded into the Ridge process.
They can perform actions during import, construction, inspection, or capability
execution and are not contained by the resource path boundary.

## Durable state

Ridge does not manage provider credentials, but this does not mean it cannot
store secrets. Job arguments, staged write content, output, and errors may
contain sensitive data. Metadata and logs have no automatic expiration. Ignore
`.ridge/` (and any custom job directory) in your own repository and review
artifacts before publishing. See [retention](guides/jobs.md#retention-and-sensitive-data).

Job cancellation verifies shutdown only of the owned local worker group. It is
not a containment boundary, rollback, or proof of remote/detached-process termination.
An uncertain outcome is `lost`; staged payloads may remain to avoid deleting data
under a live worker. Recovery never signals a stored PID without live ownership.
Review the [current limitations](guides/jobs.md#current-limitations) before using
it to stop work with external side effects.

## MCP

The MCP server communicates over local stdio and opens no network listener.
Tool annotations help the host classify side effects but do not authorize
requests. Ridge's own permission checks run in the application layer shared by
MCP and CLI. Configure host approval and sandboxing independently.

See [Authorization](concepts/authorization.md) for exact policy semantics,
enforcement guarantees, and known bypasses.
