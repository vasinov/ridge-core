# Configuration

A Ridge configuration is a YAML inventory of uniquely named resources:

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

Permissions apply to operations performed through Ridge. Resource discovery and
inspection remain available and expose configured properties plus supported and
allowed operations. See [Authorization](concepts/authorization.md) for enforcement
and the relationship to downstream permissions.

See the resource-specific pages for complete semantics:

- [Local](resources/local.md)
- [Docker](resources/docker.md)
- [SSH](resources/ssh.md)
- [S3](resources/s3.md)
