# CLI

The `ridge` command is a Typer frontend over the same application service used
by MCP. Select the [workspace configuration](configuration.md#workspace) with
`--config PATH` or `RIDGE_CONFIG`; the default is `./ridge.yaml`.

```text
ridge --config PATH config validate [--json]
ridge resources
ridge inspect RESOURCE
ridge exec RESOURCE -- ARGV...
ridge copy RESOURCE:PATH RESOURCE:PATH
ridge list RESOURCE [PATH] [--limit N] [--cursor TOKEN]
ridge read RESOURCE PATH
ridge write RESOURCE PATH (--text TEXT | --from FILE)
ridge stat RESOURCE PATH
ridge delete RESOURCE PATH [--recursive] [--background] [--idempotency-key KEY]
ridge jobs list [--limit N] [--cursor TOKEN]
ridge jobs inspect JOB_ID
ridge jobs logs JOB_ID [--stream stdout|stderr] [--offset N]
ridge jobs cancel JOB_ID
ridge locks acquire RESOURCE:OPERATION... [--lease-seconds N] [--wait-seconds N]
ridge --lock-token TOKEN locks renew
ridge --lock-token TOKEN locks release
ridge locks list [--limit N] [--cursor TOKEN]
ridge locks inspect ID
ridge locks force-release OPERATION_ID --reason TEXT
```

Use `ridge COMMAND --help` for frontend options and output details.

`config validate` uses the runtime loader without constructing the application
service, probing targets, or creating Ridge state. It reports resolved config/state
paths and effective grants; exit `0` means valid and `2` reports the first error.
`--json` emits either a success summary or `{"valid": false, "error": "..."}` on
stdout. See [configuration validation](configuration.md#validate-an-inventory)
for the output contract, trusted-provider boundary, and sensitive diagnostics.

Configured resource operations automatically claim their resources and exit 2 on
contention. `locks acquire` returns JSON including a session `id` and secret `token`.
Put the global `--lock-token TOKEN` before the operation command, or set
`RIDGE_LOCK_TOKEN`, to use that reservation across CLI invocations. The token is
also required for renewal and release. Inspection/listing never return tokens.
See [coordination](guides/coordination.md) for release, expiry, and uncertain work.
`ridge resources` and `ridge inspect` show both supported and policy-allowed
operations. An authorization denial is an expected Ridge error and exits with
status 2 before the target capability is invoked.

Commands are always argument vectors. Ridge never adds an implicit shell:

```bash
ridge exec local -- sh -lc 'printf "%s\n" "$PWD"'
```

The exit status from `ridge exec` is the child exit status. Timeout returns
status 124. Standard output and standard error remain separate. Foreground
execution buffers them until completion and prints the full output, unlike MCP's
bounded presentation. Use background local execution for incremental log reads.

Data commands use filesystem paths or exact object keys according to resource
addressing. `list` returns a JSON page (`addressing`, `entries`, `next_cursor`);
`stat` returns filesystem or object metadata. See [data semantics](concepts/resources.md).

`delete` returns JSON with an `outcome` of `deleted`, `missing`, or `acknowledged`.
Nonempty directories require `--recursive`; filesystem roots and S3 prefix
deletion are unsupported. See [deletion](concepts/resources.md#deletion) for
symlinks, partial failure, and versioned objects.

Add `--background` to `exec`, `write`, `delete`, or `copy` to submit an
immediate durable job. Add `--idempotency-key KEY` when an agent may retry the
same submission. The command prints `submitted JOB_ID`; use the `jobs`
subcommands to reconnect to it.

`jobs cancel` records durable intent and waits a bounded interval for shutdown.
Inspect its returned status: `cancelled` confirms owned-local-group termination,
`lost` means uncertainty, and a nonterminal result means cancellation is still
pending. Cancellation does not roll back writes or prove remote termination.

Execution has no timeout by default in either foreground or background mode.
Pass `--timeout SECONDS` to bound it explicitly.

`--cwd` is relative to the resource root. Execution is noninteractive.
`write` also accepts bytes from stdin when neither `--text` nor `--from` is given;
all three direct-write forms buffer the payload. Use `copy` for large files.

`jobs list` returns a JSON page (`jobs`, `next_cursor`) of authorized summaries,
newest first, with a default limit of 50 and maximum of 200. Pass `--cursor TOKEN`
to continue. `jobs inspect` emits JSON including results and
errors; a `succeeded` execution
job can still have a nonzero `result.exit_code`. See [Background jobs](guides/jobs.md)
for log paging, retention, semantic configuration checks, and cancellation limits.
