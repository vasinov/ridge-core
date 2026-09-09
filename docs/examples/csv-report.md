# A sales report across resources

A single agent—or a child with delegated access—can move an analysis between
named resources and bring back a report. This walkthrough focuses on that task
body; [delegated handoff](delegation.md) shows how a parent assigns access first.

> Run the sales analysis from `inputs` on `worker`. Save the report in `reports`
> and tell me the revenue by region.

For this request, assume `inputs` contains `sales.csv` and `analyze.py`,
`worker` has Python 3.11+, and `reports` is writable. The
[local setup](../getting-started.md) provides these resources and bundled files.

## With an agent

Connect your agent to [Ridge's MCP server](../mcp.md) using that inventory. Here
is an illustrative plan using the actual tool names; the agent chooses its calls
based on each result:

1. Call `list_resources` to check the available resources and allowed operations.
   Copy needs `data.read` on the source and `data.write` on the destination;
   running the analysis needs `compute.exec` on `worker`. Reading the final
   report through MCP needs both `data.stat` and `data.read` on `reports`.
2. Call `copy` for `inputs:sales.csv` to `worker:sales.csv`, then for
   `inputs:analyze.py` to `worker:analyze.py`. Check both calls succeeded.
   Copies return transfer metadata, not file contents.
3. Call `execute(resource="worker", argv=["python3", "analyze.py"],
   timeout_seconds=30)`. Check `result.exit_code` is `0`; on failure, inspect
   stdout/stderr before proceeding so an older report is not mistaken for a new one.
4. Copy `worker:report.csv` to `reports:report.csv`, then call
   `read_data(resource="reports", path="report.csv")`. Use the returned totals
   in the answer and include the saved resource location.

For the bundled input, the answer is East: 100.00 and West: 200.00, with the
report at `reports:report.csv`. Only the small report and command output need
to enter the conversation. The same tool calls work with a local, Docker, or SSH
worker; its configuration determines where the program runs.

For a longer analysis, add `background=true` and an `idempotency_key` to
`execute`. Retain the returned `job.id`, inspect it with `inspect_job`, and read
output with `read_job_logs`. Retrieve the report after status `succeeded` and
`result.exit_code == 0`. A later client can use that ID to pick up observation.

If other agents share the worker, reserve the task's resource/operation scopes
across the copy/run/retrieve sequence. Ordinary calls lock only their own
operation, not the gaps between calls. See [managed caller sessions](../guides/coordination.md#managed-caller-sessions)
for host-side renewal and token attachment. A busy resource is a decision point
for the caller, not an automatically queued task.

## Run with a Docker worker

Prerequisites: complete getting started, keep its Python environment active, and
have a running Docker daemon and permission to create a disposable container.
The image below has the required Python interpreter. Run from the demo directory
and choose an unused container name.
The Docker command may download the image.

```bash
docker run -d --name ridge-csv-demo --workdir /workspace python:3.11-slim sleep infinity
```

Change only the `worker` entry in `ridge.yaml` (keep inputs, reports, and grants):

```yaml
  worker:
    provider: docker
    container: ridge-csv-demo
    root: /workspace
    python: python3
```

Finish configuration edits before submission. These are the same resource
operations an agent would use, expressed through the CLI:

```bash
ridge resources
ridge inspect worker
ridge copy inputs:sales.csv worker:sales.csv
ridge copy inputs:analyze.py worker:analyze.py
ridge exec worker --background --timeout 30 --idempotency-key csv-report-1 -- python3 analyze.py
```

The last command prints `submitted JOB_ID`. Substitute that ID below:

```bash
ridge jobs inspect JOB_ID
ridge jobs logs JOB_ID
ridge jobs logs JOB_ID --stream stderr
```

Inspect again until the job is terminal. Only retrieve the report after status
`succeeded` **and** `result.exit_code` is `0`. Docker output appears on completion,
not incrementally. A fast job may be complete by the first inspection.

```bash
ridge copy worker:report.csv reports:report.csv
ridge read reports report.csv
```

Expected report and stdout are identical to the local run:

```text
region,revenue
East,100.00
West,200.00
```

The two inputs should be unchanged and stderr empty. Idempotency keys deduplicate
identical retries, not new runs: use a new key for a deliberately new attempt.
No changes to configuration are needed to reconnect from another client.

When finished, remove **only the disposable container you created above**:

```bash
docker rm -f ridge-csv-demo
```

This deletes its writable layer, including worker files. Reports remain local;
job metadata and logs remain under `.ridge`. Do not use this cleanup on an
existing shared container. See [retention and cancellation limits](../guides/jobs.md).

## What this looks like without Ridge

For an isolated local CSV, ordinary Python is sufficient. A direct Docker workflow
for the same foreground task is also short:

```bash
docker cp inputs/sales.csv ridge-csv-demo:/workspace/sales.csv
docker cp inputs/analyze.py ridge-csv-demo:/workspace/analyze.py
docker exec -w /workspace ridge-csv-demo python3 analyze.py
docker cp ridge-csv-demo:/workspace/report.csv reports/report.csv
```

Run these while the disposable container exists, as an alternative to the Ridge
steps. Check the execution exit status before copying the report. Direct Docker
copy already keeps payloads out of model context.
For one foreground Docker task, these commands may be all you need.

The extra work appears when endpoints and lifecycle requirements vary:

| Concern | Direct tooling | Ridge |
| --- | --- | --- |
| Local, Docker, SSH, or S3 endpoints | Choose `cp`, `docker cp`, `scp`/`sftp`, or cloud CLI commands and their path/quoting rules | Use named `RESOURCE:PATH` locations and `copy` |
| Two remote endpoints | Arrange a stream or local staging with the relevant clients; handle failures on both sides | Stream through the Ridge host with endpoint preflight and destination staging |
| Reconnect after submission | Use a process manager or arrange detached execution, durable stdout/stderr, exit-status recording, IDs, and polling | Submit once and inspect a durable job ID; observe bounded log pages |
| Retry after a lost response | Arrange deduplication before starting another attempt | Reuse an idempotency key for an identical submission |
| Inspect permitted operations | Consult separate configuration and policy sources | Discover supported and Ridge-allowed operations in one response |

For example, `docker exec -d` returns before the command finishes, but does not
by itself provide this workflow's durable result record and reconnectable job
log interface. A shell wrapper or existing process manager can supply those.
Likewise, a carefully written SSH/S3 transfer script can stream without whole-file
local staging. Ridge packages that plumbing so the model need not generate,
write to disk, debug, and explain it for each combination. The agent can focus on
the analysis and its results while reusing the same resource operations.

Ridge also has costs: resource configuration, a trusted local runtime and relay,
Python in Docker/SSH workers, and [current job limitations](../guides/jobs.md#current-limitations).
Native tools remain a good fit for a single fixed backend.

## SSH variant (illustrative)

With an existing POSIX account, verified host key, noninteractive OpenSSH access,
and Python installed, configure `worker` with `provider: ssh`, the host, an
absolute existing root, and its Python executable as shown in the
[SSH guide](../resources/ssh.md). Then reuse the Ridge commands above with a new
idempotency key. This is a configuration recipe, not a credential-free walkthrough.
Remote logs appear after completion; local transport cancellation does not prove
the remote command stopped.
