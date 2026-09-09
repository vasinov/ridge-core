# Resource coordination

## Why locking matters for multiple agents

Agents working in parallel can otherwise overwrite each other's inputs, replace
a directory while another agent is using it, or run conflicting commands in the
same worker. Ridge provides cooperative resource locks across CLI and MCP callers
within a [workspace](../configuration.md#workspace), so each agent does not need
to invent backend-specific coordination glue.

Ordinary calls protect one operation at a time. A multi-step task needs an explicit
session: for example, reserve a worker before copying inputs, keep it reserved
while running the program, and retrieve its output before releasing it. Without
that session, another agent could acquire the worker between those calls. Managed
caller sessions handle renewal during long operations and model reasoning.

S3 read/stat/write/delete and copy endpoints lock exact objects, so operations on
different keys can overlap. Filesystem operations and compute still lock whole
resources: two writes to different files in one resource conflict. Shared reads
can coexist. Prefer separate working areas for independent tasks; use matching
lock keys for aliases or overlapping roots.

## Coordination boundary

Ridge coordinates a workspace's participating CLI and MCP callers through a shared
local SQLite database. `state.directory` defaults to `.ridge` beside the configuration and
contains `state.sqlite3` plus job and operation artifacts. Keep state outside copied
or replaced/deleted trees and on a local filesystem. Separate state directories do not
coordinate, including when their inventories refer to the same remote targets.

Each resource optionally declares `lock_key`, defaulting to its name. Equal keys
within one state directory share coordination. Configure equal keys for aliases
or overlapping roots; Ridge does not infer physical identity. The
[security model](../security.md) defines the participation boundary.

Reads, lists, and stats take shared claims. Writes, deletion, and execution take exclusive
claims. Copy atomically takes shared source and exclusive destination claims for
the full transfer and cleanup lifetime, combining identical claims at the stronger
mode. Ordinary calls acquire temporary claims and fail
immediately on contention. Background jobs are not queued waiting for claims.
Deletion sessions must declare `data.delete`, including for background submission;
`data.write` ownership alone does not authorize or declare deletion.

Explicit sessions reserve a complete set of resource/operation pairs atomically.
Reservations remain whole-resource even for S3 and scoped data views. Independent
object operations within the same session may overlap, but outsiders still conflict
with its whole-resource reservation. There is no narrow reservation API.
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

When using [task access](../guides/delegation.md),
sessions and operations belong to the bound scope. Only that scope can use or
renew its session token; it does not transfer authority to another child. Scoped
inspection/listing shows only the caller's subtree and still checks operation
grants. Scope closure stops admission and renewal but does not release outstanding
reservations; admitted work, idle lease expiry, and explicit operator recovery
retain their normal semantics. Access-scope IDs appear in lock inspection.

Pure request checks run before admission: an equal-location copy rejection creates
neither an operation claim nor a background job. Once backend dispatch begins,
an error alone does not prove absence of effects or safe release.

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

A session protects a sequence only if callers use its token for every
participating operation.

### Action-defined footprints

A claim has a `domain` (the configured lock key), `scope` (an opaque component
array or `null` for the whole domain), and shared/exclusive `mode`. Equal or
ancestor scopes overlap; overlap conflicts when either claim is exclusive.
Scope components are not universally filesystem paths.

For S3 the complete bucket-relative key, including configured and delegated
prefixes, is one component: `["datasets/task-a/result.csv"]`. Thus exact objects
`a` and `a/b` are independent, not parent and child. S3 listing takes a whole-domain
shared claim, so it conflicts with any write in that domain. Prefix reservations
are not supported. Aliases targeting the same full key obtain identical scopes.

Narrowing requires compatible coordinate mappings for every configured resource
sharing the domain, including resources hidden from a delegated caller. Built-in
S3 aliases must name the same bucket; different prefixes are supported. Mixed,
unknown, or incompatible mappings keep the entire domain whole-resource. Unsupported
actions also retain whole-domain claims. No extra configuration or caller-supplied
footprint is needed. Permissions are checked independently of this planning.

Background jobs persist their admitted footprints. Before dispatch a worker checks
that its current plan fits those claims; changed coverage fails instead of silently
acquiring more locks. Uncertain operations retain their original footprint, so
unrelated objects can proceed while the affected target remains blocked.

Lock inspection returns `claims` as a list, for example:

```json
[{"domain":"artifacts","scope":["runs/task-a/result.csv"],"mode":"exclusive"}]
```

These canonical coordinates can include parent prefixes, not just view-relative
paths. Treat managed state and inspection output as operational metadata.

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
this view for participating calls.

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
