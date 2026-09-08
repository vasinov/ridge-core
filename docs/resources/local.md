# Local resources

A `local` resource combines compute with a rooted filesystem:

```yaml
resources:
  local:
    provider: local
    root: .
```

Use exact permissions on a `local` resource for files-only access:

```yaml
resources:
  project-files:
    provider: local
    root: ./project
    properties:
      purpose: source-code
permissions:
  project-files: [data.list, data.read, data.write, data.stat]
```

Without permissions, local resources allow both execution and data operations.
The root defaults to the configuration
directory when omitted; configured roots must already exist.

Filesystem paths are relative to the configured root. Absolute paths and paths
resolving outside the root fail; contained paths such as `nested/../file` are
accepted. Writes create missing parents and replace existing regular files or
symbolic links. Directories and special
files are rejected at file destinations.

Commands run as the Ridge operating-system user with its ambient environment and
can access anything that user can access.
Use native isolation for untrusted code; see [filesystem boundaries](../security.md#filesystem-boundaries).
Background local execution writes stdout and stderr to durable logs while the
command is running.

Execution receives an argument vector, preserves stdout/stderr and child exit
status, and invokes no implicit shell. `--cwd` is relative to the resource root.
There is no timeout unless requested and no interactive session support. Reads
and writes preserve bytes but buffer content; use [copy](../guides/copying.md)
for streaming.
