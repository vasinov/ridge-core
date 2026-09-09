# Architecture

## Overview

Ridge maps a configured resource identity and explicit operation to a composed
capability implementation. Local, Docker, SSH, and S3 exercise those semantics
across process, container, network, filesystem, and object-storage boundaries.
Cross-resource transfer coordinates compatible endpoints without exposing
backend mechanics. CLI and MCP are coequal frontends over the same application
service, and installed providers can add resource implementations without
changing Ridge core.

## Ownership

A [workspace](configuration.md#workspace) names the inventory, selected policy,
and managed state already owned by configuration, authorization, and jobs/coordination.
It adds no runtime container or configuration schema. Participating frontends use
the same authoritative configuration; resources may span multiple backends.

- `model` owns backend-neutral values and operation names.
- `resource` owns capability protocols and the typed capability collection.
  Resource identity is separate from the compute, filesystem, storage, and
  transfer and optional deletion implementations it composes.
- `backends` implement mechanisms and translate backend failures into Ridge
  errors. Remote command transports compose the shared helper protocol and
  operations rather than duplicate capability semantics.
- `registry` owns resource identity and lookup.
- `provider` owns provider registration and installed entry-point discovery.
  `config` validates the inventory envelope and asks the selected provider to
  validate and construct each resource.
- `application` owns frontend-neutral workflows, capability selection,
  authorization, and copy coordination over a registry. Foreground and background
  copy share location parsing, authorization context, validation, and scope
  preparation; workers revalidate before execution.
- `jobs` owns durable job metadata and atomic completion/claim settlement;
  `_job_runner` owns the supervisor, worker, and internal process entry point.
  `_job_process` shares process-local ownership context and handoff timing/encoding.
- `cli` and `mcp` are separate frontends over `application`; backend classes do
  not depend on either. Typer owns command declaration and the official MCP
  Python SDK owns stdio protocol behavior.

## Invariants

- Supported operations describe implemented capabilities, not permissions.
- Generic operations retain the same semantics across supporting backends.
- Backend differences are explicit errors or metadata, never silent semantic
  changes.
- Filesystem operations validate relative paths against a configured root.
  These checks do not contain hostile concurrent filesystem mutation; see
  [filesystem boundaries](security.md#filesystem-boundaries).
- Compute commands are argument vectors; no backend adds an implicit shell.
- Descriptive properties identify their configured or detected source and do
  not imply permissions.
- Expected user, configuration, backend, and boundary failures are stable Ridge
  errors rather than backend exceptions where practical.

## Decisions

- Ridge serves one trusted operator with multiple cooperating agents. The
  [security model](security.md) owns ambient-authority and sensitive-state guidance.
- Configuration selects a `provider`, not a resource kind. `local`, Docker,
  and SSH expose compute and filesystem mechanisms; S3 exposes object storage.
  Files-only local access uses exact data grants without `compute.exec`.
  There is no built-in `filesystem` provider.
- Configuration roots are resolved relative to the configuration file so the
  same configuration is independent of the invocation directory.
- CLI configuration validation calls the authoritative loader directly, not the
  application service. It summarizes loaded policy without property inspection,
  resource operations, or Ridge state initialization. Provider imports/construction
  remain trusted code; validation is not a connectivity or safety audit. The
  [configuration reference](configuration.md#validate-an-inventory) owns its output.
- Reads and writes are binary-safe. Frontends decide how to encode or bound
  content for their transport. Built-in bounded reads request at most the limit
  plus one detection byte from their streams and reject overflow, independently
  of earlier size metadata. MCP checks returned body size again before decoding
  or inlining it.
- Filesystem writes create missing parents and replace an existing regular file
  or symbolic link by default. They reject directories and special files.
- `data.delete` is an independently optional capability requiring data addressing.
  The [data contract](concepts/resources.md#deletion) owns exact-target, recursive,
  missing-result, and failure semantics. Application authorization and exclusive
  admission precede deletion. Foreground and job workers invoke the same mechanism;
  deletion never stages, rolls back, or retries. Shared standard-library deletion
  code runs locally and is prepended to the Docker/SSH helper source. S3 uses a
  deletion client with SDK retries disabled and no version ID or prefix expansion.
- Execution is synchronous and unbounded by default; callers may request an
  explicit timeout. Selected mutating operations may instead be submitted to
  the durable local job supervisor. Ridge still has no interactive execution
  sessions.
- A Docker resource refers to an existing running container. Ridge never
  creates, starts, stops, or removes that container.
- Docker execution and filesystem operations use bundled helper source through
  an explicitly configured Python executable in the container. The helper
  enforces timeouts inside the container and applies rooted filesystem checks
  without assuming a shell or image utilities. Target helpers require Python
  3.11+ and use only its standard library.
- Killing the host-side `docker exec` client does not reliably stop its
  container process. Docker timeouts are therefore enforced by the in-container
  helper; a transport timeout is reported as resource unavailability rather
  than falsely claiming execution cancellation.
- SSH resources use the system OpenSSH client so host aliases, included
  configuration, agents, certificates, proxies, and other ambient OpenSSH
  behavior remain outside Ridge. Ridge forces noninteractive authentication and
  strict host-key checking; it never accepts or persists a host key.
- SSH targets POSIX remote command semantics. OpenSSH submits a command
  string to the remote login shell, so the SSH transport applies POSIX-safe
  quoting only to bootstrap the Ridge helper. Caller commands remain argument
  vectors inside the helper protocol and are never interpolated into that shell
  command.
- The helper source, wire protocol, response decoding, error translation, and
  capability operations are shared by Docker and SSH. Each resource owns only
  its transport, configuration, inspection, and availability behavior. This is
  the internal transport seam beneath the public capability and provider API.
  Python helpers under `backends/_scripts` are packaged and loaded as
  source; the shared deletion mechanism is also imported locally. They use only the standard library and
  run through the configured target Python; targets do not need Ridge installed.
- Cross-resource copy is a coordinator operation over two resource locations,
  not a capability attributed to either endpoint. Files stream as raw bytes;
  directory trees use an uncompressed tar stream. The standard library owns
  archive framing while Ridge owns rooted-path, member, source-snapshot,
  staging, cancellation, and publication policy.
- Copy destinations are exact and missing ancestors are created automatically.
  A file replaces an existing file or symbolic link. A tree replaces an entire
  existing tree rather than merging with it, so destination-only entries are
  removed. File/tree type mismatches and special-file destinations fail. Content
  is staged beside filesystem destinations before replacement, with rollback
  attempted if publication fails. The helper records publication intent before
  moving user data. Abort removes only disposable stages or acknowledged rolled-back
  stages with no retained backup. Failed rollback, unknown phase, and unconfirmed
  publication retain recovery artifacts; published destinations are never rolled
  back by cleanup. Commit is one-attempt and requires a valid acknowledgement.
  Transport uncertainty prevents concurrent abort against a possibly live commit.
  Before streaming, the destination helper persists a receiving phase and announces
  its token. It reports a matching stopped token only after payload writes end;
  success and failed staging are distinct from publication. Cancellation closes
  unbuffered payload input and drains reports for up to two seconds before transport
  shutdown. Automatic abort requires both acknowledged staging completion and a
  disposable persisted phase, and always preserves retained backups. SIGTERM during
  receiving allows the helper to record failed staging and report its stop.
  Phase metadata supports manual recovery, not power-loss transactional durability.
  The transport tracks staging evidence (unconfirmed, stopped, finished) separately
  from publication (not attempted, failed, unconfirmed, published). Collected pipes
  and caller cancellation are independent of these outcomes. A stop report alone
  permits neither publication nor deletion of a protected persisted phase.
  The [copying guide](guides/copying.md) owns recovery instructions.
- Tree copy accepts regular files, directories, and relative symbolic links
  whose resolved targets exist inside the copied tree. It rejects absolute,
  broken, escaping, and top-level links, plus hard links and special files.
  Basic permission bits are preserved; ownership, timestamps, ACLs, extended
  attributes, sparsity, and other metadata are not.
- The copy coordinator adds no total-size or wall-clock limit; backend limits
  still apply. Streaming bounds payload memory. File exports read to EOF and then
  verify the source snapshot; tree members use snapshotted sizes. Connection
  timeouts still apply, and cancellation follows the acknowledged-cleanup rules
  above. MCP bounds model-facing results rather than changing copy semantics.
- The product, import package, and CLI are named `Ridge` and `ridge`. The Python
  distribution is named `ridge-core` because the `ridge` distribution name is
  already occupied.
- Public documentation uses MkDocs with Markdown source, Material for MkDocs,
  and mkdocstrings for the Python extension API. Documentation dependencies do
  not enter the runtime dependency set.
- The configuration file defines the resource inventory; resource locations
  identify data within it.
- Object storage and rooted filesystems retain separate mechanism contracts,
  exposed through shared `data.list/read/write/stat` and optional `data.delete` operations.
  A resource selects at most one addressing model. Discovery reports that
  model and optional copy support, including for data-only installed providers.
  Object keys and prefixes are never normalized as filesystem paths.
- An S3 resource identifies a bucket and, optionally, a key prefix and region.
  Region is resource-scoped because one inventory can contain buckets in
  different regions; when omitted, Boto3's ambient region selection applies.
  Credentials and profile selection remain in Boto3's ambient credential chain.
- A direct storage write is a native whole-object put: it creates or replaces
  the object without an `overwrite` flag. Cross-resource copy likewise replaces
  an existing object. Publication uses last-writer-wins semantics, not conditional
  version checks; cooperative claims mediate participating Ridge callers.
- Copy payloads distinguish a single file-like byte stream from a filesystem
  tree archive. Filesystem resources accept both payloads; S3 resources accept
  only single-object payloads. Directory-to-prefix mapping is not implicit.
- S3 uploads use bounded sequential parts when a stream exceeds the single-put
  buffer. Multipart upload is a correctness and bounded-memory mechanism, not a
  provider-side transfer optimization; ordinary failure/cancellation cleanup
  attempts to abort uploads. Abrupt termination can leave unfinished uploads.
  Fixed 8 MiB parts and the 10,000-part guard impose the current
  [streamed upload ceiling](resources/s3.md#streamed-upload-size).
- Object keys are relative to the resource's configured prefix but are not
  normalized as POSIX paths. Storage listing is flat, recursive by prefix, and
  explicitly paginated with opaque continuation tokens.
- MCP is a local stdio frontend with a separate `ridge-mcp` entry point. It uses
  the existing YAML inventory and ambient process authority; Ridge does not add
  an MCP-specific configuration or authorization domain.
- The MCP surface keeps compute, data, and copy operations
  explicit rather than collapsing them into a generic provider tool. The
  official MCP Python SDK v2 owns protocol framing and schema publication.
- CLI and MCP data lists share an addressing/entries/next-cursor envelope.
  Filesystem cursors encode directory-scoped offsets over fresh sorted listings,
  not snapshots; mutation can skip or repeat entries. Storage retains backend
  cursors and prefix matching. Filesystem enumeration still buffers metadata.
- MCP inline payloads and execution output are bounded for model context. Small
  UTF-8 reads are inline, and binary or oversized reads return descriptors directing
  callers to copy. Execution bounds stdout and stderr independently while reporting full
  byte counts and truncation. Resource listing exposes concise capability
  summaries; detailed properties require explicit inspection. These presentation
  limits do not bound captured execution output in memory or all response metadata.
- MCP read-only, idempotent, and destructive annotations describe likely side
  effects for the host. They are hints and never substitute for authorization.
- CLI data verbs are root commands: `list`, `read`, `write`, `stat`, and `delete`.
  MCP uses `list_data`, `read_data`, `write_data`, `stat_data`, and `delete_data`; both frontends
  dispatch through the same authorized application operations. Stat and list
  retain filesystem/object metadata. Direct reads/writes buffer content;
  foreground and background copy stream payloads through Ridge, outside model
  context, with destination staging rather than server-to-server transfer.
- A resource exposes a typed `ResourceCapabilities` collection. Registry
  inspection derives supported operations from that collection, and the
  application resolves capability implementations through it. Backends may use
  one object for several capabilities or compose separate objects.
- Installed distributions add providers through the `ridge.providers`
  Python entry-point group. The entry-point name is the configuration `provider`;
  its callable owns provider-specific validation and construction. Built-in
  names cannot be shadowed, duplicate registrations fail, and returned name and
  `provider_name` must match the requested identity. Before inventory publication,
  returned resources must expose `ResourceCapabilities` and callable
  `inspect_properties`, independently of permissions. Loading checks the envelope
  without invoking inspection or exercising provider behavior.
- The provider API extends resource implementations, not Ridge's operation
  vocabulary. New generic capabilities remain core design changes so CLI/MCP
  schemas, semantics, and authorization metadata cannot silently drift.
- Provider code is trusted in-process Python with ambient Ridge authority.
  Authorization mediates requests at the centralized application and
  capability-resolution boundary, but it cannot contain malicious provider
  internals.
- Canonical operations expose read, write, or execute effect metadata plus
  idempotence. Transfer is an optional mechanism requiring data support, not a
  separate permission family. Copy preflights source `data.read` and destination
  `data.write`; the latter includes whole-tree replacement. Descriptive copy
  support does not imply that policy allows either endpoint.
  Pure copy-location validation follows authorization and precedes foreground
  claim or background job admission. Filesystem equality uses normalized POSIX
  paths on one named resource; object keys remain exact. Backend-dependent checks
  stay inside the admitted operation, whose failure retains existing uncertainty
  rules. Direct coordinator callers use the same location validation.

## Authorization

The [delegated task access design](concepts/authorization.md#delegated-task-access-design)
records the accepted contract separately from the current
process-wide policy below. Delegation and granular lock footprints are not implemented.

`authorization` owns policy decisions; `application` checks them before invoking
capabilities through either frontend. Copy checks both endpoints before opening
either. Requests retain path, key, and argument context, while the built-in policy
matches exact resource/operation pairs. Tests verify denials and absence of target
side effects. Discovery and inspection stay outside policy.

The [authorization guide](concepts/authorization.md) owns grant semantics,
discovery visibility, and the distinction between support, permission, and
downstream authority. The [security model](security.md) owns compute bypasses
and trusted-provider assumptions.

## Durable jobs

Jobs and resource coordination share one authoritative SQLite database,
`state.sqlite3` under `state.directory` (default `.ridge` beside the configuration).
Job artifacts occupy per-job directories beside it. Retention and handling of
earlier development state are documented in the [jobs guide](guides/jobs.md).

Ridge supports immediate, durable background execution. `compute.exec`,
`data.write`, `data.delete`, and cross-resource copy may be submitted in the background. Submission
is an option on the existing operation; only observation and cancellation live in the `jobs`
namespace. Every submission receives one attempt without automatic retry.

SQLite is authoritative for job identity, request metadata, state transitions,
idempotency keys, and results. Per-job files hold stdout, stderr, and transient
staged write payloads. The configured state directory is relative to the Ridge
configuration by default. Metadata and logs have no automatic retention;
terminal-attempt cleanup attempts to remove staged write payloads. Crashes or
filesystem failures can leave payloads behind.

Each submission launches one detached local supervisor and records exactly one
attempt. The supervisor atomically claims the row within 30 seconds while holding
a per-job advisory lock. Observers reconcile expired unclaimed submissions; late
or duplicate supervisors cannot execute them. There is no recovery daemon or retry.
A separate worker receives permission to run through an inherited pipe; EOF before
handoff means no execution. From a single configuration read, the worker compares
semantic identities for the submitted resource set and the resolved state directory
before provider discovery/construction. Identities hash provider configuration,
effective lock key, and configuration directory; they preserve scalar types and
sequence order but ignore mapping order and YAML presentation. Unrelated resources
and permissions are not identity inputs. The worker constructs its service from
that same checked document and rechecks the underlying operation grants before
invoking a capability. Ambient credentials, provider
code, and downstream state can still change between submission and execution.
Idempotency includes only the referenced identities, not the entire inventory or
policy, so harmless edits do not turn an identical retry into a different request.

Write content is snapshotted into the job directory before submission returns.
Copy instead stores resource locations and opens the source when the attempt
starts; its ordinary source snapshot begins then, so Ridge does not promise the
source is unchanged since submission. Background execution does not accept
explicit environment values to avoid intentionally persisting environment secrets.
Arguments, payloads, and logs can nevertheless contain sensitive content.

Job access inherits the operations recorded in its authorization scopes rather
than adding a second job policy vocabulary. A job is listed, inspected, logged,
or cancelled only when the current policy allows every underlying scope. A copy
therefore requires both its source and destination grants. This keeps the policy
exact but intentionally cannot distinguish permission to perform an
operation from permission to observe its job metadata.

Job discovery returns bounded summary pages (default 50, maximum 200), ordered
by immutable submission timestamp and ID descending. The manager scans indexed
metadata in bounded batches; the application supplies the current authorization
decision before page filling. Terminal results are not loaded for discovery.
Opaque cursors identify a position and resolved state-directory path, not a
snapshot or permission grant. Newer jobs require restarting discovery, while
status and authorization remain live. Only returned jobs undergo lifecycle
reconciliation. Full results, errors, and cancellation intent belong to inspection.
Pagination does not impose retention or bound the total scan through hidden jobs.

States are `starting`, `running`, `succeeded`, `failed`, `cancelled`, and
`lost`. A nonzero child exit is a successfully completed execution attempt and
its exit code remains in the job result. `lost` covers expired startup, lost
supervisor ownership, or unverified termination, with an explicit error. The
kernel-held ownership lock, not a potentially recycled stored PID, establishes
whether a supervisor is present.

The supervisor remains outside the worker process group. Cancellation persists
intent before signalling; it allows five seconds after SIGTERM, then SIGKILL and
five seconds for verification. The owned child is not reaped until signalling
finishes, preventing reuse of its process-group identity. POSIX `ps` verifies that
no non-zombie group members remain; inspection failure is uncertainty, not success.
Built-in transfer helpers inherit the worker group in background mode, retaining
their separate sessions for foreground Ctrl-C handling.

Workers publish an atomic outcome file; only the supervisor publishes terminal
job state after group shutdown. Completion and cancellation intent serialize in
SQLite: terminal outcomes are immutable, and cancellation that wins before
completion is recorded takes precedence. Pending cancellation survives client
disconnect. Worker errors retain their primary message and bounded secondary
exception notes through CLI/MCP serialization and job outcomes. Cancellation
preserves reported worker diagnostics after shutdown. Cleanup failures remain
visible in `error`; uncertain termination retains staged payloads. Supervisor
loss does not trigger speculative signalling.
Completion receives an explicit local-termination assessment. Verified shutdown or
a fenced unstarted attempt permits payload cleanup but does not imply remote termination.
Cleanup failure does not erase verified termination; remote failures still retain
claims. Job outcome and claim settlement remain in the same SQLite transaction.
There is no claim of rollback, remote termination, or containment of deliberately
detached processes. See [job limitations](guides/jobs.md#current-limitations).

Local execution streams stdout and stderr into durable log files while it runs.
Current Docker and SSH helpers return output when their operation finishes, so
their job logs become available at completion. Log reads are byte-offset and
bounded, allowing reconnecting clients to page without placing an unbounded
stream in model context.

Background support is domain metadata on canonical operations, projected
through resource inspection only when the service has a job manager and the
operation is currently allowed. `compute.exec`, `data.write`, and `data.delete` support
background submission. Direct writes use one `write` job kind and resource/path
request shape regardless of addressing. `copy` also supports background
submission as an application workflow, with source-read and destination-write
job scopes. Deletion uses a `delete` job kind with resource, path, and recursive
request fields and a bounded outcome result. A background read operation is not provided.
Foreground and background execution have the same timeout semantics and no
timeout by default.

## Resource coordination

The application owns authorization and admission; `coordination` owns persistent
sessions, resource claims, operation registration, lease reconciliation, and
recovery evidence. Providers do not acquire remote locks. Every configured CLI/MCP
data/compute/copy workflow participates; direct backend use and services without
configured state remain outside coordination.

Lock identity is the shared state directory plus a resource's `lock_key`, defaulting
to its name. Aliases and overlapping resources must explicitly share keys. Resource
operations determine shared/exclusive modes; authorization remains operation-specific
and is checked before admission and again on each call. A session reserves all
declared pairs atomically, without upgrades. Its token does not confer permissions.

Operations are registered before dispatch, including within explicit sessions.
Conflicting operations in the same session cannot overlap. Session closure stops
admission but retains the complete reservation while registered work remains.
Idle leases expire on subsequent observations; expired ownership cannot resume.
Foreground ownership uses a local advisory file only as evidence of a live process;
loss marks durable claims uncertain, never automatically stopped. Known synchronous
local data failures release claims; interrupted execution and remote failures are
conservative uncertainty. Ownership files are not deleted during normal release.

Job insertion and claim admission share one transaction, including idempotency
lookup. A worker validates its existing claim and never reacquires an expired
session. Claims release with fenced unstarted attempts, acknowledged success after
local group shutdown, or verified local-only termination. Remote cancellation and
lost supervisors retain uncertainty. Force-release is explicit, authorized, limited
to uncertain operations, and records a reason without signalling processes.

See [Resource coordination](guides/coordination.md) for the complete caller contract,
lease bounds, bounded discovery, and external-access limitations.

`sessions` owns optional caller-side managed scopes: a synchronous Python renewal
thread and an asyncio MCP-client renewal task. They use the existing acquisition,
renewal, and release contract; they do not change persisted lease semantics or
renew merely because a server is alive. The host owns workflow lifetime. Bound
calls check local health before admission; the state store remains authoritative.
The first renewal error or missed local deadline permanently fails that helper,
with no retry, reacquisition, rollback, or cancellation of admitted work. Exit
stops renewal and attempts release; errors remain visible without masking an
existing body exception. A live but hung host can keep renewing. MCP hosts must
explicitly integrate token injection and keep their event loop responsive.
