# Getting started

Turn a small sales CSV into a regional revenue report using three named resources.
This walkthrough needs no cloud account, Docker, or third-party analysis library.

## Install

From a Ridge source checkout, create and activate an environment with Python
3.11 or newer. These commands use a POSIX shell (macOS or Linux):

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
ridge --help
```

The distribution is named `ridge-core`; the import and command are `ridge`.
For development instead, use `uv sync` and `. .venv/bin/activate`.
Keep the environment active so `ridge` and `python3` are available on `PATH`.

## Create the demo

Run the following from the checkout root, using a fresh `ridge-demo` directory.
The bundled [inventory](examples/assets/ridge.yaml) defines `inputs`, `worker`,
and `reports` as local resources. Create every root before discovery.

```bash
mkdir -p ridge-demo/inputs ridge-demo/worker ridge-demo/reports
cp docs/examples/assets/ridge.yaml ridge-demo/ridge.yaml
cp docs/examples/assets/sales.csv docs/examples/assets/analyze.py ridge-demo/inputs/
cd ridge-demo
ridge resources
ridge copy inputs:sales.csv worker:sales.csv
ridge copy inputs:analyze.py worker:analyze.py
ridge exec worker -- python3 analyze.py
ridge copy worker:report.csv reports:report.csv
ridge read reports report.csv
```

Both execution and the final read print:

```text
region,revenue
East,100.00
West,200.00
```

The [analysis script](examples/assets/analyze.py) uses Python's standard library
and decimal arithmetic. The [input](examples/assets/sales.csv) contains four sales.
Copies of the input and script are now in `worker/`, and identical `report.csv`
files are in `worker/` and `reports/`. Inputs are unchanged. Rerunning these
copy/analysis steps replaces their exact destinations.

## What the resource names buy you

The caller chooses locations and operations, not backend transfer commands.
Every resource is local here to keep the first run reproducible. Ordinary Python
is simpler for summing an isolated local CSV. Ridge becomes useful when the
worker is a container or SSH host, while the caller's workflow stays the same.

`ridge resources` shows supported and allowed operations. Inputs have data-only
read grants; the worker allows execution and data access. These are Ridge policy
checks, not a sandbox around commands. Use `ridge inspect worker` for detailed
properties. See [Authorization](concepts/authorization.md).

Ridge loads `./ridge.yaml` by default. Use `ridge --config PATH COMMAND` or
`RIDGE_CONFIG` to select another inventory. Relative roots resolve from the
configuration file, not the invocation directory.

## Platform expectations

Ridge requires Python 3.11+ on a POSIX host. Current acceptance evidence covers
a macOS host, Linux Docker workers, loopback POSIX OpenSSH, and S3—not every
Python/platform combination. Jobs additionally require local advisory locks and
a compatible `ps`; see [job prerequisites](guides/jobs.md). Docker/SSH workers
require a compatible Python 3 interpreter.
See [Development](development.md) for verification and external-test prerequisites.

## Next steps

- Continue with the [Docker worker and without-Ridge comparison](examples/csv-report.md).
- Explore [other examples](examples/index.md) for ML, builds, science, and media.
- Connect an agent through [MCP](mcp.md), using the same resources and operations.
- Read [copy semantics](guides/copying.md), [jobs](guides/jobs.md), and the
  [security model](security.md) before moving real data.
