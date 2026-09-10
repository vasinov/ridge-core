# Changelog

Release notes describe supported behavior and important changes. The repository's
development guide owns versioning, preparation, publication, and recovery.

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
  state, file/tree reservations, sessions, filesystem footprints, and exact S3
  object footprints. Independent files in one directory can overlap; protected
  resolution falls back broadly where complete effects cannot be established.
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
