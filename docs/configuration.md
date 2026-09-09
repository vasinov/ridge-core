# Configuration

## Workspace

A **workspace defines your resource mesh**: the resources agents can access,
the permissions they can delegate, and the shared state that coordinates their work.
The YAML configuration selects these parts; resources themselves may live on
different machines or services. A workspace
is not necessarily a directory, repository, or agent conversation.

Use the same configuration for participating CLI and MCP callers. There is no
workspace creation command or additional file format. State is initialized when
needed by runtime workflows, not by configuration validation. The configuration
establishes the initial authority. Agents [derive task access](guides/delegation.md)
for their children without separate inventories or per-task policy edits.

The inventory contains uniquely named resources:

```yaml
resources:
  source:
    provider: local
    root: ./input
    lock_key: source-files

  build:
    provider: docker
    container: ridge-build
    root: /workspace
    python: python3

  host:
    provider: ssh
    host: build.example.com
    user: ridge
    root: /srv/ridge/workspace
    python: python3

  artifacts:
    provider: s3
    bucket: example-artifacts
    prefix: ridge/runs
    region: us-west-2

permissions:
  source:
    - data.list
    - data.read
    - data.stat
  build:
    - compute.exec
  artifacts:
    - data.list
    - data.read
    - data.stat
    - data.write

state:
  directory: .ridge
```

The default path is `./ridge.yaml`. `--config PATH` takes precedence over the
`RIDGE_CONFIG` environment variable.

## Validate an inventory

With Ridge installed, run:

```bash
ridge --config /absolute/path/to/ridge.yaml config validate
ridge --config /absolute/path/to/ridge.yaml config validate --json
```

Validation uses the same loader as runtime commands, including provider construction,
local root and configured SSH file checks, and exact permission validation. It exits
`0` for a valid inventory or `2` for the first error; fix that error and rerun.
It does not create missing directories, initialize Ridge job/coordination state,
call resource operations or property inspection, or probe connectivity. A valid
inventory does not establish backend availability, credential validity, downstream
permissions, safe state placement, or correct alias lock keys.
Installed providers are trusted Python code: their imports and constructors are
not sandboxed and may have their own side effects.

The success summary shows resolved configuration and state paths, permission mode
(`exact` or `unrestricted`), and each resource's name, provider, lock key, and
effective allowed and delegable operations. Delegability is the intersection of
`permissions` and `delegation`. Validation is operator-only; bound tasks use
`access inspect` for their effective authority.
The summary does not dump raw YAML or resource properties.
Text success goes to stdout; text errors go to stderr. `--json` emits one object
on stdout for either outcome:

```json
{"valid": true, "config": "/project/ridge.yaml", "state_directory": "/project/.ridge", "permission_mode": "exact", "resources": [{"name": "inputs", "provider": "local", "lock_key": "inputs", "allowed_operations": ["data.read", "data.stat"], "delegable_operations": []}]}
```

Failures have `valid: false` and an `error` string instead of a partial inventory.
CLI argument errors retain the CLI's normal usage output. Provider-written output
is outside this format contract. Diagnostics can include paths, offending values,
YAML excerpts, or provider error text; neither output mode guarantees secret
redaction. Review before sharing.

## Agent-assisted setup

The repository-owned `ridge-setup` skill, in `skills/ridge-setup/` at your
installed release's tag, guides an agent from your workflow to an explicit workspace
configuration. Give the agent that skill directory (or ask it to read its
`SKILL.md`), the installed Ridge executable, the intended configuration path,
and the actual targets and operations you want.
For example: “Use ridge-setup to let me read this input directory and run analysis
in that workspace, without granting deletion.” Client-specific installation and
plugin packaging are separate from this skill; it is not automatically installed
by the Python package.

The agent needs host file access: Ridge MCP cannot edit its own inventory.
The skill preserves existing edits, explains explicit grants, checks state placement
and overlapping-resource lock keys, and runs validation before any separately
authorized smoke test. It does not provision infrastructure or copy credentials.
YAML remains authoritative; review the configuration diff and granted access.

## Paths and runtime configuration

Relative local paths, SSH identity files, and SSH known-hosts files are
resolved relative to the configuration file. Resource-specific configuration
is validated by the selected `provider`; built-in providers reject unknown fields.
Resource names begin with an ASCII letter or digit and contain only letters,
digits, `.`, `_`, and `-`. All built-ins accept optional scalar-valued `properties`
for descriptive metadata; these do not grant capabilities or permissions.

S3 uses Boto3's ambient credential chain; SSH uses OpenSSH configuration and agents.
Local commands inherit the Ridge process environment. Docker and SSH commands
inherit their remote helper's environment, not automatically the local Ridge
environment. Explicit request values supplement or override the execution
environment. Ridge does not manage provider credentials.

The optional `state.directory` selects durable SQLite state, logs, and transient
staged payloads. A relative value is resolved from the configuration file. The
default is `.ridge` beside that file. Background execution rejects
explicit environment values so Ridge does not persist them.
Arguments, logs, results, and staged content may still contain secrets supplied
by callers or commands. See [job retention](guides/jobs.md#retention-and-sensitive-data).

Background jobs tolerate comments, formatting, and unrelated valid configuration
edits. Changes to a referenced resource's provider configuration or lock identity,
or the workspace's state location, reject its pending attempt. Required grants are
rechecked before execution. See [job configuration checks](guides/jobs.md#status-and-configuration).

All configured operations participate in resource coordination. An optional
resource `lock_key` defaults to its resource name and uses the same character set,
with a maximum of 128 characters. Give overlapping resources the same key. Different
inventories coordinate only when they share both the state directory and matching
keys. Read [Resource coordination](guides/coordination.md) for sessions and recovery.

## Permissions

Omitting `permissions` selects unrestricted trusted mode. Supplying it enables
an exact, default-deny policy: only the listed operation names are allowed for
each listed resource. An empty map (`permissions: {}`) denies every resource
operation. Unknown resources, unknown operations, duplicate grants, and grants
for unsupported operations are configuration errors.
Built-ins support `data.delete`; grant it explicitly only where cleanup is intended.
It requires no `data.stat` grant. Keep `state.directory` outside trees callers can
delete or replace; the example inventory deliberately adds no deletion grants.

Permissions apply to operations performed through Ridge. Operator discovery and
inspection expose all configured resources and properties plus supported and
allowed operations. Scope-bound discovery shows only the task's granted resources.
See [Authorization](concepts/authorization.md) for enforcement
and the relationship to downstream permissions.

## Delegation policy

The optional `delegation` map uses the same exact resource/operation vocabulary
and validation as `permissions`. Omission or `{}` disables delegation, including
when ordinary permissions are unrestricted. It does not change ordinary use grants.
Effective delegable operations must also be allowed by `permissions`; a configured
delegation entry alone cannot authorize use or issuance beyond that policy.

```yaml
permissions:
  inputs: [data.read, data.stat]
delegation:
  inputs: [data.read]
```

Here only `data.read` is delegable; `data.stat` is usable but not delegable.
For children that need MCP inline reads, delegate both read and stat.
Configuration validation reports the effective intersection without creating state.
The map is the operator ceiling for `scope create` and the equivalent MCP tool.
Task handles bind resource subsets and optional narrower data roots without
rewriting the inventory. See [task delegation](guides/delegation.md).

See the resource-specific pages for complete semantics:

- [Local](resources/local.md)
- [Docker](resources/docker.md)
- [SSH](resources/ssh.md)
- [S3](resources/s3.md)
