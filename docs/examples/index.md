# Examples

Ridge is most useful when an agent must move artifacts between different
execution or data environments, act on them, and reconnect to the result.
It supplies resource discovery, common operations, streamed copy, and job
observation—not the domain program or its dependencies.

## Runnable walkthrough

[Managed caller sessions](../guides/coordination.md#managed-caller-sessions) includes
a read-only MCP-host example that maintains ownership across several lease periods.

[Sales CSV to regional report](csv-report.md): start with the credential-free
[local quickstart](../getting-started.md), then change the worker to Docker and
observe a background run. Includes expected outputs, cleanup, and a direct-tool
comparison. No third-party analysis library is required.

## Illustrative recipes

The recipes below are shapes for adapting your own programs, not copy-and-paste
benchmarks. Resource names, scripts, data, and dependencies must already exist.
Discover supported and allowed operations first. After submitting a job, inspect
its status and command exit code before retrieving outputs; substitute its actual
job ID in the [job observation commands](../guides/jobs.md).

### ML experiments

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
bounded logs, and copy metadata. Ridge neither provisions GPUs nor schedules
experiments. Multi-file datasets need explicit file copies or a prebuilt archive;
an object prefix is not a filesystem tree.

### Builds and tests

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
inside that destination tree. Put reusable dependencies elsewhere. Ridge does
not install dependencies, implement CI triggers, or infer whether tests passed.

### Scientific computing

Prerequisites: an SSH `lab` host with a solver and licenses installed, local
`inputs` and `reports`, and a single input file.

```bash
ridge copy inputs:parameters.json lab:parameters.json
ridge exec lab --background -- solver --input parameters.json --output result.csv
# After successful completion:
ridge copy lab:result.csv reports:result.csv
```

The agent can reconnect to a durable attempt instead of holding a long shell
session. Ridge is not an HPC batch scheduler and does not replace Slurm or manage
licenses. Confirm host policy before executing directly on shared infrastructure.

### Media processing

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
retained indefinitely, and cancellation does not confirm local or remote process
termination. See [copying](../guides/copying.md), [jobs](../guides/jobs.md), and
[security](../security.md) before adapting these to valuable data.
