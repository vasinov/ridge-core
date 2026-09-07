# Background jobs

Execution, data writes, and copy can run as durable
background jobs. The operation itself remains in its ordinary CLI command or
MCP tool; the `jobs` namespace is only for lifecycle management.

```console
$ ridge exec local --background -- python -c 'print("done")'
submitted 8ad3e1d4-...
$ ridge jobs inspect 8ad3e1d4-...
$ ridge jobs logs 8ad3e1d4-...
done
```

Jobs start immediately and receive one attempt. Ridge does not provide queues,
priorities, dependencies, schedules, retries, or worker routing. SQLite stores
metadata and results under `jobs.directory`; per-job files store logs. Direct
write content is staged before submission returns; terminal cleanup attempts to
remove it. Copy sources are opened only when execution begins.

Use an idempotency key when a caller may lose the submission response and retry.
The same key and identical request return the existing job. Reusing the key for
different content or parameters fails.

Local execution logs are readable while the command runs. Docker and SSH helper
output currently becomes readable after completion. Log reads are bounded and
MCP returns a next byte offset for polling. CLI prints the raw page bytes; advance
`--offset` by the number of bytes received and use `--limit` to bound each read.

## Status and configuration

States are `starting`, `running`, `succeeded`, `failed`, `cancelled`, and `lost`.
`succeeded` means Ridge completed the operation, not that an executed command
returned zero: inspect `result.exit_code` before using its outputs. `failed`
reports a Ridge/provider failure (including an execution timeout); `lost` means
a recorded supervisor disappeared without a terminal result.

The supervisor reloads the original configuration and rejects the attempt if
its byte fingerprint changed, even for a formatting-only edit. It rechecks the
underlying grants. Credentials, installed provider code, and downstream data are
not snapshotted. Background execution rejects explicitly supplied environment
values; it still inherits ambient credentials and environment.

## Current limitations

Cancellation sends SIGTERM to the recorded local supervisor process group and
marks the attempt cancelled. It does not wait for verified group termination or
escalate resistant descendants. A cancelled status is not proof that side effects
have stopped, even locally. For Docker/SSH it also cannot establish termination
of processes beyond the local transport. Use an explicit execution timeout where
appropriate; remote helpers enforce that timeout at the execution site.

A crash between durable submission and recording the supervisor PID can leave a
job `starting` indefinitely. Idempotent resubmission returns that same job; it is
not an automatic recovery or retry mechanism. Do not blindly resubmit under a
new key: first inspect whether the original work produced side effects.

Job listing currently fetches all authorized jobs without pagination. Application
and MCP return full records; CLI prints ID/kind/status/submission-time rows.
Unlike data listings and log reads, its response grows with history.

## Retention and sensitive data

Metadata, results, idempotency keys, and stdout/stderr logs are retained
indefinitely under `jobs.directory` (default `.ridge/jobs` beside the config).
There is no automatic expiration, pruning, or deletion command. Terminal cleanup
attempts to remove staged write payloads; crashes and filesystem errors can leave
them behind. Copy requests reference their source rather than staging its bytes
at submission, so subsequent source changes can affect the attempt.

Arguments, errors, logs, results, and staged content can contain secrets or
private data. Keep job state out of Git and public artifacts, restrict access,
and monitor disk use. Before any manual archival or cleanup, establish that no
supervisor or descendant is still using it; a cancelled status alone is
insufficient. Do not delete job data as a code-upgrade step.

## Authorization

The job inherits every underlying resource operation grant. Copy has both a
source and destination scope. Current policy must still allow all scopes to
list, inspect, read logs, or cancel that job.

Writes have kind `write` and a `data.write` scope for either addressing model.
Copy records source `data.read` and destination `data.write`. Job formats are
active-development contracts without migrations. To retain an earlier
development version's job data, leave its directory intact and select a new
`jobs.directory` for the current version.
