# Configuration

A Ridge configuration is a YAML inventory of uniquely named resources:

```yaml
resources:
  source:
    provider: local
    root: ./input

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

jobs:
  directory: .ridge/jobs
```

The default path is `./ridge.yaml`. `--config PATH` takes precedence over the
`RIDGE_CONFIG` environment variable.

Relative local paths, SSH identity files, and SSH known-hosts files are
resolved relative to the configuration file. Resource-specific configuration
is validated by the selected `provider`; unknown fields fail rather
than being silently ignored.

Ridge does not manage or intentionally persist provider credentials. S3 uses
Boto3's ambient credential chain, SSH uses OpenSSH configuration and agents, and local or Docker commands
inherit the Ridge process environment unless a request explicitly supplies
environment values.

The optional `jobs.directory` selects durable SQLite state, logs, and transient
staged payloads. A relative value is resolved from the configuration file. The
default is `.ridge/jobs` beside that file. Background execution rejects
explicit environment values so Ridge does not persist them.
Arguments, logs, results, and staged content may still contain secrets supplied
by callers or commands. See [job retention](guides/jobs.md#retention-and-sensitive-data).

## Permissions

Omitting `permissions` selects unrestricted trusted mode. Supplying it enables
an exact, default-deny policy: only the listed operation names are allowed for
each listed resource. An empty map (`permissions: {}`) denies every resource
operation. Unknown resources, unknown operations, duplicate grants, and grants
for unsupported operations are configuration errors.

Permissions apply to operations performed through Ridge. Resource discovery and
inspection remain available and expose configured properties plus supported and
allowed operations. Ridge does not alter downstream operating-system or service
permissions and does not prevent access outside Ridge.

See the resource-specific pages for complete semantics:

- [Local](resources/local.md)
- [Docker](resources/docker.md)
- [SSH](resources/ssh.md)
- [S3](resources/s3.md)
