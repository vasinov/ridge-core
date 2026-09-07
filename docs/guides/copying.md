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

Filesystem destinations are published only after both endpoints finish and the
source still matches its initial snapshot. Failed publication attempts rollback.
Ordinary failure/cancellation cleanup removes staging and still-empty ancestors
created by the copy when possible; abrupt process termination or cleanup errors
can leave artifacts. S3 multipart cleanup attempts to abort unfinished uploads.
See [job cancellation limits](jobs.md#current-limitations).

Copy has no arbitrary total size or wall-clock limit. Transport connection
timeouts still apply. Streaming avoids a complete temporary payload on the Ridge
host when relaying between remote resources, but filesystem destination staging
requires space for the incoming file/tree. Existing destination data can also
remain on disk until publication completes. S3 multipart parts are not a local
whole-file spool. This is neither zero-disk I/O nor a direct cloud-to-cloud copy.
