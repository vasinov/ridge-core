# Copying between resources

```bash
ridge copy SOURCE_RESOURCE:PATH DESTINATION_RESOURCE:PATH
```

Copy supports single files among filesystem and S3 resources and directory
trees among filesystem resources. Both locations are exact: Ridge does not
change the destination based on whether it looks like a directory.

A file replaces an existing file or symbolic link. A tree replaces the entire
existing destination tree rather than merging with it, so stale
destination-only entries are removed. File/tree mismatches fail. Missing
ancestors are created automatically, and filesystem destinations are staged
before publication.

Trees preserve regular-file bytes, empty directories, basic permission bits,
and relative symbolic links whose targets exist within the copied tree.
Absolute, broken, escaping, and top-level links are rejected, as are hard links
and special files. Ownership, timestamps, ACLs, extended attributes, sparse
layout, and other metadata are not preserved.

Foreground and background copy stream payloads through the Ridge host, not
through model context. Ridge relays bytes; this is not a server-to-server copy.
Payload memory is bounded (64 KiB relay chunks; S3 buffers sequential 8 MiB
upload parts). Tree metadata and filesystem destination staging are separate
from that payload-memory bound. Direct read/write operations are buffered.

Copy checks source `data.read` and destination `data.write` before opening either
endpoint. The write grant includes replacement of whole filesystem trees,
including removal of destination-only entries. It is not an append-only grant.

Equal source/destination locations on the same named resource are rejected before
claims or background jobs are created. Filesystem comparison uses normalized POSIX
paths; object keys use exact strings. This check does not infer physical identity
across aliases or resolve symbolic links. Backend checks still run under claims.

Filesystem destinations are published only after both endpoints finish and the
source still matches its initial snapshot. Failed publication attempts rollback.
If rollback fails, Ridge retains staging and the previous destination at
`STAGING/replaced` for manual recovery. Publication/recovery errors identify the
resource, known publication phase, and recovery paths. A failure after publication leaves the
new destination in place; it does not roll back an already-published result.

An interrupted or missing commit acknowledgement leaves publication unconfirmed.
Ridge does not retry publication or automatically clean up that attempt. Inspect
the destination and any remaining staging before retrying the copy or removing
artifacts; a reported path may already be gone if publication and cleanup finished
before the response was lost. There is no automatic recovery or backup expiration.
Follow [coordination recovery](coordination.md#inspecting-abandoned-work) for any
uncertain claims; retained artifacts and operation ownership are separate concerns.

Before publication, failure/cancellation cleanup attempts to remove disposable
staging and still-empty ancestors created by the copy. Filesystem helpers announce
their staging token before payload streaming. On interruption, Ridge closes the
payload input and allows two seconds for a report that staging has stopped before
stopping the transport. Cleanup requires a matching stop report and a disposable
phase; stopping a Docker/SSH client alone does not establish that its writer stopped.
An unconfirmed stop retains any staging and reports the known resource-relative
path. Abrupt termination before the token arrives can leave unidentified staging;
cleanup errors can also leave artifacts. S3 multipart cleanup attempts to abort
unfinished uploads. Secondary cleanup errors accompany
the primary failure in CLI/MCP errors and background job inspection, with bounded
diagnostics and explicit truncation. See [job cancellation limits](jobs.md#current-limitations).

The coordinator adds no total-size or wall-clock limit; backend limits and
transport connection timeouts still apply. In particular, the current
[S3 destination limit](../resources/s3.md#streamed-upload-size) is 78.125 GiB per
streamed object. Streaming avoids a complete temporary payload on the Ridge
host when relaying between remote resources, but filesystem destination staging
requires space for the incoming file/tree. Existing destination data can also
remain on disk until publication completes. S3 multipart parts are not a local
whole-file spool.
