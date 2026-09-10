# Changelog

Release notes describe supported behavior and important changes. The repository's
development guide owns versioning, preparation, publication, and recovery.

## 0.2.0

- Publish independent files concurrently within one local, Docker, or SSH resource.
  Supported filesystem operations protect file or tree paths while conflicting
  operations, directory observations, and tree replacement remain coordinated.
- Reserve individual files or trees across multiple calls through Python
  `LockRequest`, CLI `RESOURCE:OPERATION:PATH`, or MCP's optional reservation
  `path`. S3 reservations cover exact objects. Omit the path to keep existing
  whole-resource sessions.
- Validate filesystem resolution under protection before effects. Background jobs
  retain their admitted claims and fail before dispatch if those claims no longer
  cover the operation. Repeated idempotent submissions return the existing job
  before attempting new filesystem preparation.
- Fix managed MCP sessions to include their reservation token in `delete_data`
  calls, allowing authorized deletion within the session.
- Let providers pair footprint planning with optional read-only
  `FootprintGuardCapability` validation under admitted claims.
- Clarify delegation and reconnect workflows, distinguishing assignments, access
  scopes, job outcomes, and lock sessions. Align the overview and setup skill with
  these workflows.

**Upgrade from 0.1.0:** configuration and persisted state formats are unchanged;
no migration or new state directory is required. Existing APIs and whole-resource
reservations remain supported. Finish active work and restart participating Ridge
clients and workers together when upgrading; mixed-version workspace operation is
not a supported upgrade procedure. Preserve existing state and recovery artifacts.

**Limits:** compute retains whole-resource protection. Filesystem narrowing requires
supported paths and compatible resource coordinates; symlinks, hard links, nested
mounts, missing parents, and other unsupported cases retain broad protection.
Explicit path reservations fail when narrow protection cannot be established or
an operation exceeds the reservation. See the
[coordination guide](https://vasinov.github.io/ridge-core/guides/coordination/)
for supported paths and tree limits.

## 0.1.0

Initial public release of Ridge, a resource mesh for AI agents, for Python 3.11+
on macOS and Linux.

- Define named local, Docker, SSH, and S3 resources in a YAML workspace. Validate
  configuration before connecting and extend resources through Python providers.
- Use a shared Python API, CLI, and stdio MCP server for resource discovery,
  bounded reads, writes, metadata, listing, exact deletion, and compute execution.
- Stream files and filesystem trees between resources, with staged publication,
  cancellation handling, and explicit recovery information for uncertain outcomes.
- Delegate attenuated access to child tasks, narrow data locations, bound further
  delegation, set expiry, revoke scopes, and supervise descendant jobs.
- Run durable background jobs with reconnectable status, logs, cancellation,
  and paginated discovery. Coordinate participating operations with shared managed
  state, resource reservations, sessions, and exact S3 object footprints.
- Connect agent clients using documented Codex, Claude, desktop, VS Code, and
  LangChain recipes, local plugin packaging, and executable delegation examples.
- Publish versioned wheel/source distributions through verified release automation.
- Keep the public documentation and manual rebuilds on the latest final release;
  check development documentation independently before publication.

Ridge mediates participating resource operations; it is not a sandbox or agent
orchestrator. Native infrastructure permissions still apply. Docker/SSH workers
need Python 3.11+. State must remain on a suitable local filesystem; failed or
interrupted transfers may require inspection of retained recovery artifacts.
See the security, copying, and jobs guides before operating on valuable data.

This initial release establishes the first published configuration and persisted
state contracts. Earlier development snapshots have no migration guarantee; keep
old state intact and select a new state directory when necessary. During 0.x,
minor releases may break compatibility and will describe upgrade requirements.
