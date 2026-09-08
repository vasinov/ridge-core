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

## Managed caller sessions

Python hosts can own renewal around a workflow instead of asking a model to
remember lease deadlines:

```python
from ridge import JobScope, Operation, RidgeService

ridge = RidgeService.from_config("ridge.yaml")
scopes = [JobScope("project-files", Operation.DATA_STAT)]
with ridge.lock_session(scopes) as session:
    metadata = session.service.stat_data("project-files", "README.md")
    # Model reasoning or long synchronous calls can occur within this scope.
    session.check()
```

The single-use context acquires the reservation, runs a renewal thread every
third of the lease duration, and returns a service that attaches the token and
checks session health before foreground and background admission. Cached service
views cannot be used after exit or rebound to another token. The host must use
this view for participating calls; this is not a new security boundary.

For an already-connected MCP `Client`, use the asyncio caller helper:

```python
from ridge import JobScope, ManagedMCPSession, Operation

async with ManagedMCPSession(client, [JobScope("project-files", Operation.DATA_STAT)]) as session:
    result = await session.call_tool(
        "stat_data", {"resource": "project-files", "path": "README.md"}
    )
    # Await model turns here while an independent task renews the lease.
```

The client must outlive the managed scope. Route the six resource tools
(`execute`, `list_data`, `read_data`, `write_data`, `stat_data`, `copy`) through
`session.call_tool`; use the original client for discovery, job observation, and
other control tools. The helper injects the token and rejects caller-supplied
tokens. Ordinary tool results, including errors, retain their MCP representation.
Do not block the asyncio event loop with synchronous model calls or CPU work.
Existing third-party MCP hosts must integrate this lifecycle explicitly; merely
installing the Ridge MCP server does not enable client-side renewal.

Run the bounded, read-only example from a source checkout:

```bash
uv run examples/managed_session.py --config ridge.example.yaml --resource project-files --path README.md
```

It performs three stats separated by pauses longer than its one-second lease.
The short lease is for demonstration; ordinary managed sessions default to five
minutes and accept the same lease and acquisition-wait bounds as manual sessions.
There is no new YAML setting; `state.directory` and `lock_key` apply unchanged.

Both helpers fail closed on the first renewal error or missed local deadline:
further managed calls fail, `check()` reports the failure, and context exit also
reports it. They never retry, silently reacquire, force-release, or terminate
already-admitted work. Heartbeat errors do not asynchronously interrupt arbitrary
host code; call `check()` between non-Ridge workflow steps when useful. If another
exception is already propagating, renewal/cleanup failures are attached as exception
notes rather than masking it. Otherwise failures raise at exit (multiple failures
use an exception group). MCP renewal requests time out after at most one third
of the lease, capped at ten seconds; release has a ten-second timeout.

Exit stops renewal and attempts release, including on workflow cancellation.
An interrupted acquisition without a returned token may leave an idle reservation
until its lease expires. Failed release also leaves recovery to the existing
expiry/inspection rules. A host crash stops renewal; a hung host whose heartbeat
still runs can retain ownership. Hosts own workflow lifetime, cancellation, and
overall timeouts. These helpers do not supervise independent CLI invocations.
Custom Python authorizers must tolerate renewal calls from the heartbeat thread
concurrently with workflow calls; provider execution stays on its existing path.
Active work still follows the existing expiry and conservative recovery rules;
no operation timeout or remote-termination guarantee is added.

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
