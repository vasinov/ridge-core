# Resources and capabilities

A resource is a configured identity such as `local`, `build`, or `artifacts`.
Its provider composes one or more capability implementations behind that
identity. Agents delegate views of these existing resources, not duplicate
inventories: the name stays the same while allowed operations and data roots
can differ by task. See [delegating work](../guides/delegation.md).

Resources select a `provider` (`local`, `docker`, `ssh`, `s3`, or an installed
provider). Callers use `compute.exec` and `data.list/read/write/stat/delete`, not
backend-specific operation families. Permissions determine which supported
operations are allowed; omitting permissions allows all supported operations.

Discovery reports `provider`, `addressing` (`filesystem`, `object`, or null for
compute-only resources), and `supports_copy`. Copy support is descriptive, not
a permission grant: copy checks source `data.read` and destination `data.write`.

Use `ridge resources` (MCP `list_resources`) to see supported, allowed, and
background-capable operations before acting. Use `ridge inspect NAME` (MCP
`inspect_resource`) for detailed properties and their configured/detected origin.
Background operation metadata is shown when a job manager is available and the
operation is allowed.
Invalid configuration and unknown resources fail before executing the requested
resource operation; installed providers are trusted code during loading too.

Internally, Ridge composes five mechanism contracts:

- **compute** executes an argument vector;
- **filesystem** lists, reads, writes, and stats rooted paths;
- **storage** lists, reads, writes, and stats object keys;
- **transfer** opens streaming sources and destinations used by copy.
- **delete** optionally removes exact entries using the resource's data addressing model.

Filesystem and object storage remain separate because directories, symbolic
links, path traversal, keys, prefixes, pagination, and metadata have different
semantics. A resource selects at most one data addressing model. Data-only
providers do not need compute; optional transfer support enables streamed copy.

## Shared data operations, explicit addressing

`ridge list RESOURCE [PATH]` and MCP `list_data` return `addressing`, `entries`,
and `next_cursor`. Pass the opaque cursor unchanged to request another page.
The default limit is 100; CLI/application allow up to 1000, MCP up to 200.

- Filesystem listing returns sorted immediate children, with `path`, `kind`,
  and `size`. Omitting the path selects `.`. Cursors are scoped to the resource
  and requested directory. Pagination uses offsets over a fresh listing, not
  a snapshot; concurrent directory changes can skip or repeat entries. The
  backend still enumerates the directory in memory.
- Object listing returns flat recursive prefix matches, with `key`, `size`,
  `etag`, and `modified_at`. Omitting the path selects the empty prefix. Cursors
  retain backend semantics. Use them only with the same resource and prefix.
  Keys such as `a/../b` and `a//b` remain exact keys, not normalized paths.

Read, write, and stat take the same `path` parameter in MCP/Python; for object
addressing it means an exact resource-relative key. Stat retains backend
metadata rather than inventing filesystem kinds for objects. Direct reads and
writes buffer content; use copy to stream large payloads.

With `max_bytes` (CLI `read --max-bytes`), built-in reads enforce the limit on
the body as well as checking size metadata. They request at most one extra byte
to detect overflow and raise `OutputLimitExceededError` instead of returning
partial content. An omitted limit leaves direct reads unbounded. This is a byte
limit, not a content snapshot or a read timeout.

The registry derives supported operations from the typed capability collection.
Providers do not declare arbitrary commands or MCP tools. Adding a generic
operation remains a Ridge API design decision so its semantics stay consistent
across frontends and implementations.

## Deletion

`ridge delete RESOURCE PATH` and MCP/Python `delete_data(resource, path,
recursive=False)` require `data.delete`, without an additional list, read, or stat
grant. Deletion is optional for installed providers; all built-ins support it.
It removes data within a resource, not the configured resource, container, host,
bucket, job history, or retained recovery artifacts automatically.

Filesystem deletion removes regular files, symbolic links (including broken or
escaping final links, without following their targets), and empty directories.
A nonempty directory requires explicit `--recursive` / `recursive=true`.
Recursion is an intent check, not another permission or confirmation prompt.
Parent paths must remain inside the root; the resource root itself, absolute
paths, final `..` components, and special files are rejected. During recursion,
links are unlinked and special files fail the attempt. No glob expansion occurs.

Success returns `{"outcome": "deleted"}` or `{"outcome": "missing"}` for
filesystems. S3 deletes one exact current key and returns
`{"outcome": "acknowledged"}` without a prior-existence claim or a HEAD request.
Missing targets succeed. S3 rejects `recursive=true`; it never expands a key
into a prefix. See [S3 version semantics](../resources/s3.md#deletion).

Deletion is foreground by default, with no operation timeout. Use `--background`
or `background=true` for a durable, cancellable attempt. Both modes take exclusive
resource claims and accept coordination sessions. Background requests identify
the path/key as it exists when the worker runs, not a snapshotted entry.

Recursive deletion is non-atomic. Failure or cancellation may leave a partially
deleted tree; removed data is not restored. A transport failure can leave work
running or its outcome unconfirmed. Inspect the target and
[uncertain claims](../guides/coordination.md) before retrying or force-releasing.
There is no rollback, trash, version purge, or automatic retry. A successful
result does not prevent later recreation by another writer. Background
`idempotency_key` deduplicates submission, not deletion effects.
