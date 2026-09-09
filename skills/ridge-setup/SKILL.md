---
name: ridge-setup
description: Prepare or update a Ridge YAML resource inventory from a user's workflow, explain explicit access, and validate it using installed Ridge. Use for Ridge setup and configuration repair, not infrastructure provisioning or routine resource operations.
---

# Ridge setup

Turn the user's intended workflow into a reviewed, valid Ridge workspace
configuration: resource inventory, permission policy, and managed state. Ridge
must already be installed, and you need host file access; Ridge MCP cannot edit
its own configuration. Do not assume access to a Ridge source checkout.
Workspace is the name for these existing parts, not a new file format or directory
layout. Keep participating callers on the chosen configuration and state; do not
create a separate inventory per child or invent delegated-scope commands.

## Establish the workflow

Identify the installed `ridge` and, when needed, `ridge-mcp` executables, the
chosen YAML path, existing configuration, actual targets, and intended operations.
Ask for missing target or access decisions instead of inventing hosts, containers,
buckets, paths, or grants. Setup approval is not infrastructure provisioning or
permission to test mutations on existing data. Keep credentials in the ambient
AWS/OpenSSH mechanisms, never in the inventory or conversation.

Use the installed command's `--help` and the relevant current Ridge documentation:

- [Configuration](https://vasinov.github.io/ridge-core/configuration/) for the schema,
  path resolution, validation output, and policy defaults.
- Provider details only for the selected targets:
  [local](https://vasinov.github.io/ridge-core/resources/local/),
  [Docker](https://vasinov.github.io/ridge-core/resources/docker/),
  [SSH](https://vasinov.github.io/ridge-core/resources/ssh/), or
  [S3](https://vasinov.github.io/ridge-core/resources/s3/).
- [Authorization](https://vasinov.github.io/ridge-core/concepts/authorization/)
  and [coordination](https://vasinov.github.io/ridge-core/guides/coordination/)
  when selecting grants and shared state/lock keys.
- [MCP](https://vasinov.github.io/ridge-core/mcp/) and
  [copying](https://vasinov.github.io/ridge-core/guides/copying/) for an authorized
  copy/execute/read smoke test and its frontend requirements.

If documentation and the installed CLI differ, surface the version mismatch;
do not silently upgrade, invent an option, or substitute execution for validation.

## Prepare and explain the inventory

Read before editing. Preserve unrelated resources, grants, comments, and user
changes; make a scoped patch rather than regenerating an existing inventory.
Show the resulting access and any material change to existing access. If existing
policy is unrestricted, switching to an exact map affects every resource:
confirm that change rather than silently denying unrelated workflows or copying
unrestricted access into broad new grants.

For new inventories, generate an explicit `permissions` map with only the needed
operations. Omission means unrestricted trusted mode; `{}` denies all operations.
Copy needs source `data.read` and destination `data.write`; there is no copy grant.
MCP inline reading additionally needs `data.stat`. Grant `compute.exec` only for
requested execution and `data.delete` only for requested cleanup. Explain that
execution can mutate data independently of data grants and whole-tree copy can
replace destination contents. Ridge permissions constrain Ridge calls, not direct
host access, commands' ambient authority, or other tools.

Local roots and explicitly configured SSH key/trust files must already exist for
the loader. Relative host paths resolve beside the YAML, not the launch directory.
Docker and SSH refer to existing targets with Python 3.11+; a successful validation
does not prove those targets are reachable. Use actual scoped S3 prefixes, not a
fabricated bucket. Inspect relevant provider guidance for required fields.

Keep durable `state.directory` outside trees the workflow can replace or delete.
Give aliases/overlapping scopes the same `lock_key`; inventories cooperate only
with shared state and matching keys. Do not infer remote overlap from path text
alone, move existing state, or claim validation detects these mistakes. Resolve
ambiguous shared-resource ownership with the user.

## Validate and hand off

Run `ridge --config /absolute/path/to/ridge.yaml config validate --json` using the
identified executable. Exit 0 means the runtime loader accepted it; exit 2 reports
the first error. Fix errors within the approved scope and rerun. Do not weaken
permissions or change targets merely to make validation pass. Validation does not
probe connectivity, operate on resources, or initialize Ridge state; installed
provider code remains trusted. Diagnostics may contain sensitive paths, YAML
excerpts, or provider text, so review before quoting them.

Review resolved config/state paths and each resource's effective allowed operations
against the request. When preparing delegation policy is requested, review
`delegable_operations` too: `delegation` is separately default-deny and is intersected
with ordinary permissions. Do not add delegation grants for routine setup or mistake
that validation field for an available scope-binding workflow. Consult the installed
CLI and authorization documentation before attempting delegation; the internal store
is not a substitute for frontend enforcement.

Explain what remains unverified. Run a small real workflow
only when its targets and effects are authorized; use an empty disposable scope
for mutations and inspect outputs and cleanup, not just exit status. Stop on
unresolved access decisions or uncertain effects instead of broadening grants or
retrying destructive work.

Hand off the config path, concise diff/access summary, validation result, and any
smoke-test outcome or gap. For an MCP launch, supply absolute executable and config
paths (`ridge-mcp --config /absolute/path/to/ridge.yaml`); client-specific connection
steps are separate. Do not modify agent-client settings unless requested.
