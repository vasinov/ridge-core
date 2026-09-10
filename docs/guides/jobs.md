# Background jobs

Execution, data writes, deletion, and copy can run as durable
background jobs. The operation itself remains in its ordinary CLI command or
MCP tool; the `jobs` namespace is only for lifecycle management. Jobs belong to
the [workspace's managed state](../configuration.md#workspace), not the client
conversation that submitted them. Reconnect using the same configuration.

```console
$ ridge exec local --background -- python -c 'print("done")'
submitted 8ad3e1d4-...
$ ridge jobs inspect 8ad3e1d4-...
$ ridge jobs logs 8ad3e1d4-...
done
```

Jobs start immediately and receive one attempt. Ridge does not provide queues,
priorities, dependencies, schedules, retries, or worker routing. SQLite stores
metadata and results under `state.directory`; per-job files store logs. Direct
write content is staged before submission returns; terminal cleanup attempts to
remove it. Copy sources are opened only when execution begins.
Deletion likewise targets the path/key at execution time. Its `delete` job kind
requires `data.delete` for submission (and use or delegation for descendant observation) and records an `outcome`
result; see [deletion](../concepts/resources.md#deletion). Cancelling a delete
does not restore removed entries, and remote work may continue after local shutdown.

Background supervision requires a POSIX host with local filesystem locking and
`ps` supporting `-axo pid=,pgid=,stat=` (macOS and Linux procps). Keep job state
on a local filesystem supporting SQLite and advisory locks. A supervisor owns
one separate worker process group; built-in local copy helpers stay in that group.

Use an idempotency key when a caller may lose the submission response and retry.
The same key and identical request return the existing job. Reusing the key for
different content or parameters fails.
Concurrent preparation of the same key is serialized separately from resource claims;
after 60 seconds a contender reports a conflict and can retry with that same key.

Local execution logs are readable while the command runs. Docker and SSH helper
output currently becomes readable after completion. Log reads are bounded and
MCP returns a next byte offset for polling. CLI prints the raw page bytes; advance
`--offset` by the number of bytes received and use `--limit` to bound each read.
For a `lost` job, logs may still grow: the log page's `complete` flag describes
the current terminal-record/end-of-file observation, not proof that a lost or
detached writer has stopped.

## Supervising delegated work

For [delegated tasks](../guides/delegation.md),
job ownership and idempotency keys belong to the submitting access scope. A caller
sees only its own subtree. Own jobs require current use grants; descendant jobs
allow current use or delegation grants for every underlying operation. Delegation
therefore permits supervision (including logs/results and cancellation), not direct use.
Closing a scope blocks its future submissions and result access, not work already
admitted; an authorized ancestor can inspect or cancel descendant jobs separately.

## Status and configuration

States are `starting`, `running`, `succeeded`, `failed`, `cancelled`, and `lost`.
`succeeded` means Ridge completed the operation, not that an executed command
returned zero: inspect `result.exit_code` before using its outputs. `failed`
reports a Ridge/provider failure (including an execution timeout); `lost` means
the startup handoff expired, the supervisor disappeared, or local termination
could not be verified. Inspect `error`; `lost` never proves work stopped.

The worker reads the selected configuration once. Before discovering or constructing
providers, it checks the job's referenced resource identities and resolved state
directory against submission. Comments, formatting, mapping order, unrelated valid
resource changes, and permission-list ordering do not invalidate a job. Any changed
provider configuration on a referenced resource (including descriptive properties),
provider name, or effective lock key rejects the attempt; removing a resource or
changing the state directory does too. Provider defaults and equivalent path spellings
are not normalized, so an explicit provider-option edit may conservatively reject
the attempt even when it names the same target.

Workers also verify that the current action footprint fits the persisted claims.
Changing another resource in the same lock domain can affect alias compatibility
and broaden the required footprint; that rejects an already-narrow job before
dispatch. It does not silently acquire more locks. Changes in unrelated domains
do not affect this coverage check.
Filesystem workers also revalidate path resolution under those claims. A symlink,
mount, or other change requiring broader effects fails before dispatch; jobs never
upgrade their reservation. See [filesystem coverage](coordination.md#action-defined-footprints).

Execution uses that checked document and rechecks the job's required grants.
Removing an unrelated grant is harmless; removing a required grant denies execution.
Adding grants does not change the submitted operation. Later file edits do not replace
configuration within the running attempt. Credentials, installed provider code, and
downstream data are not snapshotted. Background execution rejects explicitly
supplied environment values; it still inherits ambient credentials and environment.

## Startup and cancellation

The supervisor must claim a submission within 30 seconds. Inspection, listing,
log reads, and identical idempotent resubmission reconcile an abandoned, expired
`starting` job to `lost`. No daemon scans idle history, and recovery never retries
the operation. Atomic claiming prevents late or duplicate supervisors from running
a settled job. Cancellation before the claim prevents execution entirely.

Cancellation first records durable intent (`cancellation_requested`). The owning
supervisor sends SIGTERM to its worker group, allows five seconds for graceful
shutdown, then sends SIGKILL if needed and allows five seconds for verification.
It remains alive to record the result even if the cancelling client disconnects.
Only verified local termination produces `cancelled`; unverified termination
produces `lost` with an explanation. Zombies count as stopped, not live workers.
No recovery path signals a PID merely because it appears in the database.

Filesystem copy helpers handle SIGTERM during staging so they can report that
writes have stopped. The worker uses that report for
[staging cleanup](copying.md); forced termination can prevent its delivery.

The cancel call waits for a bounded interval (normally up to about 12 seconds,
excluding database contention). If it returns a nonterminal job with cancellation
requested, inspect again; a request is not confirmation. Already-persisted terminal
outcomes do not change. If cancellation wins before completion is recorded, the
attempt is cancelled after shutdown, even if some writes already happened.

The supervisor also closes the owned worker group before publishing ordinary
completion or failure, including any remaining descendants. Commands intended
to leave background services running are not a supported job-lifetime mechanism.

## Current limitations

Termination verification covers the owned local group, including built-in copy
helpers. It does not cover processes that deliberately leave the group, detached
processes created by providers, or Docker/SSH processes beyond the local transport.
Use an explicit execution timeout where appropriate; remote helpers enforce it
at the execution site.

A supervisor crash after execution starts is reported as `lost` on observation;
the worker may still be running. Recovery does not guess at process ownership or
kill possibly reused PIDs. Do not blindly resubmit under a new key: first inspect
whether the original work is still running or produced side effects.

## Discovering jobs

Python `list_jobs(limit=50, cursor=None)`, CLI `jobs list --limit 50`, and MCP
`list_jobs` return one page with `jobs` and `next_cursor`. Limits must be 1–200.
Each summary contains `id`, `kind`, `status`, `scopes` (resource/operation pairs),
`submitted_at`, `started_at`, and `finished_at`. Inspect a job for its full result,
error, and cancellation intent; discovery does not include these fields or requests.

```console
$ ridge jobs list --limit 20
$ ridge jobs list --limit 20 --cursor TOKEN_FROM_PREVIOUS_PAGE
$ ridge jobs inspect JOB_ID
```

Jobs sort by submission timestamp descending, then ID descending for equal
timestamps. Pass the opaque `next_cursor` unchanged to continue after the last
returned job; `null` means no more visible jobs were found. Page size may change
between calls. Cursors are tied to the resolved state-directory path; malformed
cursors or cursors from another directory fail. They are positions, not credentials.

Listing is not a snapshot. Newer submissions appear when you restart without a
cursor; status and timestamps of execution are observed live. Removed entries
do not shift continuation positions. Current policy is checked before filling
each page, including every scope of a copy job. A policy change can change which
jobs are visible; restart discovery for a complete view under the new policy.

Discovery reads metadata in bounded batches, using an index for ordering, and
does not load terminal-job results. Hidden history can still require scanning
many batches to fill a page or establish its end; page size bounds response count,
not execution time or total metadata bytes. Only visible jobs returned on a page
are reconciled for expired startup or supervisor loss. Inspect a known job ID
directly when monitoring its lifecycle.

## Retention and sensitive data

Metadata, results, idempotency keys, and stdout/stderr logs are retained
indefinitely under `state.directory` (default `.ridge` beside the config).
There is no automatic expiration, pruning, or job-deletion command. `data.delete`
is for resource data, not job retention. Staged write
payload cleanup is attempted after verified shutdown or a fenced, unstarted attempt.
Cleanup errors appear in the job's `error` field without replacing its operation
result; inspect that field even for a successful job. Worker failures retain bounded
secondary diagnostics, including copy recovery paths. Cancellation preserves those
diagnostics when the worker reports them before shutdown; forced termination may
prevent a report. See [copy recovery](copying.md) before removing retained staging.
When termination is uncertain, payloads are retained rather than deleted
underneath a possible live worker.
A crash before a submission is committed can leave an unreferenced job directory.
Copy requests reference their source rather than staging its bytes
at submission, so subsequent source changes can affect the attempt.

Arguments, errors, logs, results, and staged content can contain secrets or
private data. Keep job state out of Git and public artifacts, restrict access,
and monitor disk use. Before any manual archival or cleanup, establish that no
supervisor, detached descendant, or remote operation is still using it. Cancellation
is not rollback: published data stays published, and forced termination can leave
filesystem staging or unfinished multipart uploads. Do not delete job data as a
code-upgrade step.

## Authorization

A job records its underlying resource operation grants. Copy has both a source
and destination scope. Own-job access requires current use grants; descendant
supervision accepts current use or delegation grants for every recorded operation.
See [supervising delegated work](#supervising-delegated-work).

Writes have kind `write` and a `data.write` scope for either addressing model.
Copy records source `data.read` and destination `data.write`. Published job formats
follow the [release compatibility policy](../development.md#versioning-and-releases).
A breaking release may require a new `state.directory`; follow its upgrade notes
and leave earlier state intact. Unpublished development snapshots have no migration
guarantee. Never discard state while a worker or recovery operation may still use it.
