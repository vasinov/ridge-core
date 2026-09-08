# A sales report across resources

The [getting-started walkthrough](../getting-started.md) is the complete,
credential-free local version: four input rows become two regional totals.
This page moves the same analysis to a Docker worker and adds background
observation. The script and its output format do not change.

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

Finish configuration edits before submission. Run:

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
write to disk, debug, and explain it for each combination. That saves the tokens
spent on that glue; the analysis code and its reasoning are still required.

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
