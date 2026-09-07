# Resource coordination

Ridge coordinates participating CLI and MCP callers through a shared local SQLite
database. `state.directory` defaults to `.ridge` beside the configuration and
contains `state.sqlite3` plus job and operation artifacts. It replaces
`jobs.directory`; old state is not migrated or deleted. Keep state outside copied
or replaced trees and on a local filesystem. Separate state directories do not
coordinate, including when their inventories refer to the same remote targets.

Each resource optionally declares `lock_key`, defaulting to its name. Equal keys
within one state directory share coordination. Configure equal keys for aliases
or overlapping roots; Ridge does not infer physical identity. Coordination is
advisory at Ridge's application boundary, not a remote lock or a sandbox.

Reads, lists, and stats take shared claims. Writes and execution take exclusive
claims. Copy takes shared source and exclusive destination claims, combining equal
keys into one exclusive claim. Ordinary calls acquire temporary claims and fail
immediately on contention. Background jobs are not queued waiting for claims.

Explicit sessions reserve a complete set of resource/operation pairs atomically.
Every pair must be supported and authorized before acquisition. Calls recheck
authorization and the session's declared operations. Tokens do not grant authority.
Conflicting calls within a session also conflict. There are no incremental claims,
upgrades, or nested sessions. Acquisition supports bounded waiting up to 60 seconds.

Sessions have a five-minute idle lease by default (configurable per acquisition
from 1 to 3600 seconds). Valid operations and explicit renewal refresh it. Expired
tokens cannot be revived. Release or expiry closes admission; all session claims
remain until active operations finish. Idle sessions are reclaimed on subsequent
requests without a daemon. Release using the token remains possible after grants
change. Tokens are returned only at acquisition and stored as hashes.

Foreground operations register before dispatch. A local ownership file identifies
a live caller without trusting stored PIDs; loss marks the operation uncertain,
not stopped. Background claims and jobs share an admission transaction. Supervisor startup
fencing permits release of unstarted attempts. Verified owned local shutdown can
release local claims; remote failures or transport cancellation retain uncertainty.
Normal acknowledged completion releases claims within the documented limits on
detached processes. A terminal job status alone never establishes safe release.

Inspection exposes session/operation identities, scopes, claim keys, expiry,
status, and related job IDs, without tokens or full job results. Listings are
bounded and filtered by current grants. Force-release requires current grants for
all affected scopes and an explicit reason; it records that reason and does not
cancel execution. Inspect external effects before overriding uncertain ownership.

Arbitrary commands may modify resources they did not declare, and external tools
can bypass Ridge entirely. Separate working areas remain useful. A session protects
a sequence only if callers use its token for every participating operation.

## Example session

```bash
ridge locks acquire worker:data.read worker:data.stat worker:data.write worker:compute.exec
```

Save the returned secret token in `RIDGE_LOCK_TOKEN` for subsequent calls:

```bash
ridge read worker config.json
ridge write worker config.json --text '{"mode":"test"}'
ridge exec worker -- python3 validate.py
ridge locks release
```

Provide `--config` or `RIDGE_CONFIG` consistently. In MCP use `acquire_locks` with
the same operation pairs and supply `lock_token` on every participating call.
`renew_locks`/`ridge locks renew` extends an open lease. Renew before expiry during
long pauses or long-running operations if further calls must remain in the session;
active work retains its claims even when the session expires.

## Inspecting abandoned work

`ridge locks list` returns an authorized page of outstanding sessions and operations;
`ridge locks inspect ID` also accepts a job ID. Session and operation IDs are public
references, not tokens. Listings use UUID cursors over fresh state, not snapshots;
new or released entries can change subsequent pages. Results are bounded to 200
entries (100 by default). Records and ownership files have no automatic retention.

When an operation is `uncertain`, inspect its related job, external processes, and
destination effects. After establishing that overlap is acceptable, use
`ridge locks force-release OPERATION_ID --reason 'inspection evidence'`. Only
uncertain operations can be force-released. An open session's reservation remains
until its token is released or its lease expires, even after an operation is
force-released. Never treat recovery as rollback or termination.

Ridge uses POSIX advisory locking and SQLite on the local coordination host;
network-mounted SQLite is not a distributed coordination service. No locks need
to be acquired on SSH, Docker, or S3 targets. All processes sharing state must use
the same coordination protocol. Local state is trusted and must not be modified by
resource operations; place it outside any copied or replaced resource tree.
