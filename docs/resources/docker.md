# Docker resources

A Docker resource targets an existing running container:

```yaml
resources:
  build:
    provider: docker
    container: ridge-build
    root: /workspace
    python: python3
```

It exposes compute and rooted-filesystem operations. Prepare the container and
root directory before using it; the configured executable must be Python 3.11
or newer. Ridge sends standard-library helper source for argument-vector
execution, in-container timeout enforcement, and rooted filesystem checks.
The container does not need Ridge installed.
Data [deletion](../concepts/resources.md#deletion) removes entries under the root,
not the container, and supports foreground and background execution.

Delegated [`data_root` views](../guides/delegation.md#narrow-data-views) are
checked by the helper inside the container when used, including every inherited
symlink boundary. Scope creation does not contact or modify the container.

[Filesystem footprint validation](../guides/coordination.md#action-defined-footprints)
runs inside the container under admitted claims. Supported file/tree operations and
path reservations can overlap; compute and unsupported mappings retain broad protection.

Set `executable` when the Docker CLI is not available as `docker` on `PATH`.

The root must be absolute. Missing, stopped, or paused containers and unavailable
helper interpreters fail explicitly. Killing the local Docker client does not
prove its container command stopped; the helper enforces requested execution
timeouts inside the container. Transport timeouts are resource-unavailability
errors, not confirmation of remote cancellation.
