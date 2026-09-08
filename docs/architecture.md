# Architecture

## Overview

Ridge maps a configured resource identity and explicit operation to a composed
capability implementation. Local, Docker, SSH, and S3 exercise those semantics
across process, container, network, filesystem, and object-storage boundaries.
Cross-resource transfer coordinates compatible endpoints without exposing
backend mechanics. CLI and MCP are coequal frontends over the same application
service, and installed providers can add resource implementations without
changing Ridge core.

## Architecture

- `model` owns backend-neutral values and operation names.
- `resource` owns capability protocols and the typed capability collection.
  Resource identity is separate from the compute, filesystem, storage, and
  transfer implementations it composes.
- `backends` implement mechanisms and translate backend failures into Ridge
  errors. Remote command transports compose the shared helper protocol and
  operations rather than duplicate capability semantics.
- `registry` owns resource identity and lookup.
- `provider` owns provider registration and installed entry-point discovery.
  `config` validates the inventory envelope and asks the selected provider to
  validate and construct each resource.
- `application` owns frontend-neutral workflows, capability selection,
  authorization, and copy coordination over a registry.
- `cli` and `mcp` are separate frontends over `application`; backend classes do
  not depend on either. Typer owns command declaration and the official MCP
  Python SDK owns stdio protocol behavior.

## Invariants

- Supported operations describe implemented capabilities, not permissions.
- Generic operations retain the same semantics across supporting backends.
- Backend differences are explicit errors or metadata, never silent semantic
  changes.
- Filesystem paths are relative to a configured root and cannot escape it.
- Compute commands are argument vectors; no backend adds an implicit shell.
- Descriptive properties identify their configured or detected source and do
  not imply permissions.
- Expected user, configuration, backend, and boundary failures are stable Ridge
  errors rather than backend exceptions where practical.

## Decisions

- Ridge is initially a trusted, single-user process. It inherits ambient OS
  authority and credentials and is not a sandbox. It does not manage credentials;
  caller-provided arguments, content, and output can still persist secrets in jobs.
- Configuration selects a `provider`, not a resource kind. `local`, Docker,
  and SSH expose compute and filesystem mechanisms; S3 exposes object storage.
  Files-only local access uses exact data grants without `compute.exec`.
  There is no built-in `filesystem` provider.
- Configuration roots are resolved relative to the configuration file so the
  same configuration is independent of the invocation directory.
- Reads and writes are binary-safe. Frontends decide how to encode or bound
  content for their transport. Built-in bounded reads request at most the limit
  plus one detection byte from their streams and reject overflow, independently
  of earlier size metadata. MCP checks returned body size again before decoding
  or inlining it.
- Filesystem writes create missing parents and replace an existing regular file
  or symbolic link by default. They reject directories and special files.
- Execution is synchronous and unbounded by default; callers may request an
  explicit timeout. Selected mutating operations may instead be submitted to
  the durable local job supervisor. Ridge still has no interactive execution
  sessions.
- A Docker resource refers to an existing running container. Ridge never
  creates, starts, stops, or removes that container.
- Docker execution and filesystem operations use bundled helper source through
  an explicitly configured Python executable in the container. The helper
  enforces timeouts inside the container and applies rooted filesystem checks
  without assuming a shell or image utilities. It is a declared portability
  constraint, not an installation or persisted agent.
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
  The [copying guide](guides/copying.md) owns recovery instructions.
- Tree copy accepts regular files, directories, and relative symbolic links
  whose resolved targets exist inside the copied tree. It rejects absolute,
  broken, escaping, and top-level links, plus hard links and special files.
  Basic permission bits are preserved; ownership, timestamps, ACLs, extended
  attributes, sparsity, and other metadata are not.
- Copy has no arbitrary total-size or wall-clock limit. Streaming bounds content
  memory, the source snapshot bounds the expected payload, transport connection
  timeouts still apply, and caller cancellation aborts staged content. MCP bounds
  model-facing results rather than changing copy semantics; transfer budgets
  remain deferred until a workflow demonstrates a need.
- Identity-aware and contextual authorization remains deferred. The trust model,
  resource path boundary, and exact process-scoped permission policy are current
  product behavior rather than security polish.
- The product, import package, and CLI are named `Ridge` and `ridge`. The Python
  distribution is named `ridge-core` because the `ridge` distribution name is
  already occupied.
- Public documentation uses MkDocs with Markdown source, Material for MkDocs,
  and mkdocstrings for the Python extension API. Documentation dependencies do
  not enter the runtime dependency set.
- Jinja2 is the standard if Ridge gains human-readable text templates. It must
  not be used as a substitute for argument vectors, shell escaping, policy
  evaluation, or structured serialization. A runtime dependency is added only
  with the first concrete product template; documentation tooling may depend on
  Jinja2 independently.
- The configuration file is currently the resource-inventory scope. Ridge has
  no `Workspace` domain object. That term is reserved until real transfer and
  agent workflows show whether it should mean a named inventory plus locations;
  it will not silently imply synchronization, execution lifetime, isolation, or
  a security boundary.
- Object storage and rooted filesystems retain separate mechanism contracts,
  exposed through shared `data.list/read/write/stat` application operations.
  A resource selects at most one addressing model. Discovery reports that
  model and optional copy support, including for data-only installed providers.
  Object keys and prefixes are never normalized as filesystem paths.
- An S3 resource identifies a bucket and, optionally, a key prefix and region.
  Region is resource-scoped because one inventory can contain buckets in
  different regions; when omitted, Boto3's ambient region selection applies.
  Credentials and profile selection remain in Boto3's ambient credential chain
  and Ridge persists neither. Additional S3 configuration is deferred until a
  demonstrated workflow requires it.
- A direct storage write is a native whole-object put: it creates or replaces
  the object without an `overwrite` flag. Cross-resource copy likewise replaces
  an existing object; Ridge currently uses trusted single-user, last-writer-wins
  semantics rather than optimistic concurrency.
- Copy payloads distinguish a single file-like byte stream from a filesystem
  tree archive. Filesystem resources accept both payloads; S3 resources accept
  only single-object payloads. Directory-to-prefix mapping is not implicit.
- S3 uploads use bounded sequential parts when a stream exceeds the single-put
  buffer. Multipart upload is a correctness and bounded-memory mechanism, not a
  provider-side transfer optimization; ordinary failure/cancellation cleanup
  attempts to abort uploads. Abrupt termination can leave unfinished uploads.
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
- MCP responses are structured and bounded for model context. Small UTF-8 reads are
  inline, and binary or oversized reads return descriptors directing callers to
  copy. Execution bounds stdout and stderr independently while reporting full
  byte counts and truncation. Resource listing exposes concise capability
  summaries; detailed properties require explicit inspection.
- MCP read-only, idempotent, and destructive annotations describe likely side
  effects for the host. They are hints and never substitute for authorization.
- CLI data verbs are root commands: `list`, `read`, `write`, and `stat`.
  MCP uses `list_data`, `read_data`, `write_data`, and `stat_data`; both frontends
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
  `provider_name` must match the requested identity.
- The provider API extends resource implementations, not Ridge's operation
  vocabulary. New generic capabilities remain core design changes so CLI/MCP
  schemas, semantics, and authorization metadata cannot silently drift.
- Provider code is trusted in-process Python with ambient Ridge authority.
  Authorization mediates requests at the centralized application and
  capability-resolution boundary, but it cannot contain malicious provider
  internals; plugin sandboxing would be a separate system.
- Canonical operations expose read, write, or execute effect metadata plus
  idempotence. Transfer is an optional mechanism requiring data support, not a
  separate permission family. Copy preflights source `data.read` and destination
  `data.write`; the latter includes whole-tree replacement. Descriptive copy
  support does not imply that policy allows either endpoint.

## Authorization experiment

Ridge separates three concepts: support means an implementation exists,
authorization means Ridge policy permits a request, and downstream authority
means the operating system or service will perform it. The effective operation
is the intersection of all three.

The initial experiment uses one operator-selected policy per Ridge process. No
policy means explicitly unrestricted trusted mode; a configured policy is
default-deny and contains exact resource-and-operation grants. It has no users,
roles, explicit deny rules, path patterns, conditions, inheritance, or owner
fields. Requests retain their path, key, or argument context for evidence and
future policy without matching on that context yet.

Authorization occurs in the application layer before capability invocation and
is identical across CLI and MCP. Copy preflights `data.read` on the source
and `data.write` on the destination before opening either endpoint.
Resource discovery and inspection are intentionally outside policy in this
single-operator experiment. They expose all configured resource names, providers,
supported operations, allowed operations, and provider inspection properties.
Configuration metadata is therefore not confidential from clients able to
reach a Ridge frontend.

Ridge does not assign one ambiguous enforcement-strength label. It instead
makes separately scoped statements about its policy decision, coverage of
Ridge-owned entry points, known bypasses through other allowed operations, and
the independent downstream authority of the operating system or service. A
Ridge allow or deny decision does not imply that AWS IAM, SSH credentials, the
local operating system, or another downstream boundary implements the same
policy.

The Ridge enforcement guarantees are that application operations authorize
before invoking their target capability, CLI and MCP use that same boundary,
and copy authorizes both endpoints before opening either. Tests cover these
guarantees with denied operations and inspected side effects. Direct backend or
credential access outside Ridge remains outside this boundary.

Arbitrary compute execution can bypass filesystem mediation unless a native
account, container, operating-system, or service boundary also applies.
Installed providers remain trusted in-process code and are outside policy
containment.

## Durable jobs

Jobs and resource coordination share one authoritative SQLite database,
`state.sqlite3` under `state.directory` (default `.ridge` beside the configuration).
Job artifacts occupy per-job directories beside it. No older development state
is migrated or deleted automatically.

Ridge supports immediate, durable background execution without becoming
a scheduler. `compute.exec`, `data.write`, and cross-resource
copy may be submitted in the background. Submission is an option on the
existing operation; only observation and cancellation live in the `jobs`
namespace. There are no priorities, dependencies, schedules, worker pools,
automatic retries, or broker semantics.

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
handoff means no execution. The worker fingerprints a single read of the original
configuration before parsing or provider construction, refuses changed bytes,
and constructs its service from that same checked document. It rechecks the
underlying operation grants before invoking a capability. Ambient credentials, provider
code, and downstream state can still change between submission and execution.

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
There is no claim of rollback, remote termination, or containment of deliberately
detached processes. See [job limitations](guides/jobs.md#current-limitations).

Local execution streams stdout and stderr into durable log files while it runs.
Current Docker and SSH helpers return output when their operation finishes, so
their job logs become available at completion. Log reads are byte-offset and
bounded, allowing reconnecting clients to page without placing an unbounded
stream in model context.

Background support is domain metadata on canonical operations, projected
through resource inspection only when the service has a job manager and the
operation is currently allowed. `compute.exec` and `data.write` support
background submission. Direct writes use one `write` job kind and resource/path
request shape regardless of addressing. `copy` also supports background
submission as an application workflow, with source-read and destination-write
job scopes. A background read operation is not provided.
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
