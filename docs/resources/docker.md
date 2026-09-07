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

It exposes compute and rooted-filesystem operations. Ridge does not create,
start, stop, restart, or remove the container.

The container must contain the configured Python 3 executable. Ridge sends
ephemeral helper source for exact argument-vector execution, in-container
timeout enforcement, and rooted filesystem checks; it does not install a
persistent agent. Scratch and distroless images without a compatible
interpreter are not supported.

Set `executable` when the Docker CLI is not available as `docker` on `PATH`.

The root must be absolute. Missing, stopped, or paused containers and unavailable
helper interpreters fail explicitly. Killing the local Docker client does not
prove its container command stopped; the helper enforces requested execution
timeouts inside the container. Transport timeouts are resource-unavailability
errors, not confirmation of remote cancellation.
