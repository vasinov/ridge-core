# Examples

Give an agent a task that spans resources: train on a GPU worker, test a project
in a container, or turn a stored video into a preview. Ridge lets it discover
the available operations, move the inputs, run the program, and retrieve results
through the same interface.

It also coordinates multiple agents sharing mutable resources. Automatic locking
protects individual calls; explicit sessions keep a task's resource reservation
across calls, with automatic renewal available to adopting Python/MCP hosts.

## Start with an agent task

> Run the sales analysis from `inputs` on `worker`. Save the report in `reports`
> and tell me the revenue by region.

[Sales CSV to regional report](csv-report.md#with-an-agent) follows this request
through MCP discovery, copy, execution, and report reading. It also covers
background observation, a Docker worker, and a direct-tool comparison. The
[local quickstart](../getting-started.md) supplies runnable inputs and setup.

[Managed caller sessions](../guides/coordination.md#managed-caller-sessions) shows
how a Python MCP host keeps a multi-call reservation alive across lease periods.

## Illustrative recipes

The recipes below assume configured resources and existing scripts, data, and
dependencies. Each pairs an agent request with the core Ridge commands; an MCP
agent uses the corresponding `copy` and `execute` tools. Discover supported and
allowed operations first. After submitting a job, inspect its status and command
exit code before retrieving outputs; use its returned ID in the
[job observation commands](../guides/jobs.md).

### Multiple agents sharing a build worker

Suppose two agents use the same `builder`. Agent A reserves the required source,
builder, and report resource/operation pairs before copying its inputs, running
tests, and retrieving results. Agent B's conflicting Ridge calls fail while that
reservation is held; B can continue work on independent resources. Shared read
access to source data need not exclude other readers.

Both callers must use the same local state directory and matching resource lock
keys, and A must attach its session token to every participating call. A managed
caller session handles token attachment and renewal; it does not queue B's task
or decide when B should retry. Read [multi-agent coordination](../guides/coordination.md)
and the runnable managed-session example before adapting the recipes below for
shared workers. A sequence of ordinary CLI calls alone does not reserve the gaps
between operations.

### ML experiments

> Train a model on `datasets:train.parquet` using `gpu`, and save the model in
> `artifacts:experiments/run-001/`.

Prerequisites: a single dataset object in an S3 resource `datasets`; a local,
Docker, or SSH `gpu` worker with the right framework, GPU drivers, and training
code already installed; a writable `artifacts` resource.

```bash
ridge copy datasets:train.parquet gpu:train.parquet
ridge exec gpu --background -- python3 train.py --input train.parquet --output model.bin
# After successful completion:
ridge copy gpu:model.bin artifacts:experiments/run-001/model.bin
```

Dataset and model bytes stay outside model context; the agent sees job status,
bounded logs, and copy metadata. Multi-file datasets need explicit file copies or
a prebuilt archive; an object prefix is not a filesystem tree.

### Builds and tests

> Run the tests for `source:project` on `builder` and bring the test report back
> to `reports`.

Prerequisites: a `source` filesystem resource with a self-contained source tree,
an existing `builder` container/host with dependencies, and local `reports`.

```bash
ridge copy source:project builder:project
ridge exec builder --background --cwd project -- python3 -m pytest --junitxml=results.xml
# Inspect result.exit_code, then retrieve test results even for test failures:
ridge copy builder:project/results.xml reports:results.xml
```

Whole-tree replacement removes stale destination-only files. Use a dedicated
worker directory: replacement also removes a virtual environment or cache kept
inside that destination tree. Put reusable dependencies elsewhere and inspect
the command's exit code to determine whether tests passed.

### Scientific computing

> Run the solver on `lab` with `inputs:parameters.json` and save its results in
> `reports`.

Prerequisites: an SSH `lab` host with a solver and licenses installed, local
`inputs` and `reports`, and a single input file.

```bash
ridge copy inputs:parameters.json lab:parameters.json
ridge exec lab --background -- solver --input parameters.json --output result.csv
# After successful completion:
ridge copy lab:result.csv reports:result.csv
```

The agent can reconnect to a durable attempt instead of holding a long shell
session. Confirm host policy before executing directly on shared infrastructure;
use its required scheduler when direct execution is not permitted.

### Media processing

> Make a 1280-pixel-wide preview of `media:source.mp4` on `worker` and save it
> in `outputs`.

Prerequisites: a single video object in `media`, an existing `worker` with FFmpeg
and sufficient destination staging space, and writable `outputs`.

```bash
ridge copy media:source.mp4 worker:source.mp4
ridge exec worker --background -- ffmpeg -y -i source.mp4 -vf scale=1280:-2 preview.mp4
# After successful completion:
ridge copy worker:preview.mp4 outputs:preview.mp4
```

Large video bytes stream between endpoints without entering the conversation;
the agent need not generate an S3-download/remote-upload script. Ridge still
relays network traffic and the worker stores the files. The `-y` option explicitly
allows FFmpeg output replacement, independently of Ridge copy semantics.

## Shared boundaries

Copies require source `data.read` and destination `data.write`; execution requires
`compute.exec`. Downstream OS/service permissions must also allow the work.
Copy streams payloads; direct reads and writes buffer them. Job results/logs are
retained indefinitely. A cancellation request is not confirmation: `cancelled`
verifies owned local-group shutdown, not remote or detached-process termination.
See [copying](../guides/copying.md), [jobs](../guides/jobs.md), and
[security](../security.md) before adapting these to valuable data.
