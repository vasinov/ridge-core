# Archive workflow experiment

This bounded experiment adapts the [agent-led host](agents.md) to an ordinary
assignment: compare gzip and LZMA compression of synthetic application logs.
Local storage holds code, host state and the final report; two disposable Docker
workers run the same benchmark; AWS S3 holds the input and published artifacts.

The comparison has three modes: an experiment-only MCP adapter over ordinary
Docker/Boto3 calls, a shared Ridge operator connection, and separately bound
Ridge child scopes. It is an integration experiment, not a new public API or a
general-purpose agent harness.

## Workload and evidence

Each mode uses the same approximately 12 MiB deterministic input, benchmark
source, two containers, three compression samples, and default Codex model.
Workers run concurrently on separate containers with one CPU and 512 MiB each.
Modes run sequentially; timing therefore includes possible cache and host-load
effects. The host independently downloads each archive, decompresses it, and
checks input and artifact hashes. The model reads only small reports.

Fresh worker processes publish the results. A fresh parent receives a retained
assignment/scope/job manifest, without the previous conversation. A collector
reads metrics and writes the comparison; in scoped mode its connection has only
data grants. The scoped parent creates child grants through MCP, and the host
validates them before launching workers. Compute workers have separate resources
and results prefixes; their input and code grants are read-only.

Scoped probes cover input-write denial, sibling worker/job denial, literal S3
`..` key behavior, nonzero command exit, and revoked reconnect. A literal `..`
component is an object-key component inside the caller's prefix, not filesystem
parent traversal. Reservations are unnecessary for independent output objects.

The native baseline stages transfers locally and runs compute synchronously;
its host retains execution receipts. Ridge uses streamed copy and durable
background jobs. These differences are measured workflow choices, not proof
that native tools cannot stream or provide durable execution. The native MCP
adapter itself is bespoke setup effort. One trial per mode establishes neither
reliability nor a general performance advantage.

## Run

Use the source checkout's uv environment, authenticated Codex CLI, a running
Docker daemon with `python:3.13-slim`, and an explicitly authorized disposable
AWS S3 bucket in `us-east-1`. Ambient credentials need object read/write/delete,
bucket listing and multipart listing/abort; version inspection requires additional
permissions. The host rejects non-AWS endpoint overrides. No bucket configuration
or permissions are changed.

```bash
uv sync
uv run python integrations/archive_experiment.py setup /tmp/my-archive-trial --bucket MY_TEST_BUCKET
uv run python integrations/archive_experiment.py native /tmp/my-archive-trial
uv run python integrations/archive_experiment.py shared /tmp/my-archive-trial
uv run python integrations/archive_experiment.py scoped /tmp/my-archive-trial
uv run python integrations/archive_experiment.py cleanup /tmp/my-archive-trial
```

Setup requires a new directory and verifies an empty unique S3 prefix. Each mode
requires its own new subdirectory; failed trials are retained for inspection.
The shared-mode prompts use assignment-specific idempotency keys; scoped workers
both use `benchmark` in their independent namespaces. Add `--resume` to a Ridge
mode to reuse saved worker/publication receipts and, for scoped mode, the same
active scope handles. Only missing steps run. This is a small host recovery aid,
not general crash recovery: inspect admitted work when a receipt is missing.
Agent calls have three-minute limits and incur normal model usage; compute jobs
have 90-second limits. Containers have a two-hour lifetime, no network, and no
host mounts or AWS credentials. Inputs are about 12 MiB and normal aggregate
uploads remain below 250 MiB; this is not a cloud-account quota enforcement tool.

Keep the scratch directory private. It contains configuration, durable job state,
IDs and artifacts. Active bearer handles use a mode-600 file and are deleted after
revocation. Raw model transcripts are not saved. Per-invocation traces retain tool
names, status, elapsed time and token usage; selected operation diagnostics omit
scope/lock handles and command arguments. Outcomes retain verified metrics.

Run cleanup after failures too. It settles known jobs, revokes scopes, checks
unfinished multipart uploads, removes current run objects, and removes only
containers carrying the run's label. Unsettled jobs or claims stop artifact cleanup
for inspection. It attempts deletion of observed run-created
versions; when permission is denied, ordinary deletion can retain those versions
and add delete markers. Cleanup records known versions and unavailable version
inspection rather than claiming physical erasure. The local evidence
directory remains available for the findings report.
