# SSH resources

Supported [filesystem footprints](../guides/coordination.md#action-defined-footprints)
and path reservations permit independent data operations. Read-only validation runs
on the remote host under admitted claims; compute and unsupported mappings stay broad.

An SSH resource targets an existing POSIX account through the system OpenSSH
client:

```yaml
resources:
  build-host:
    provider: ssh
    host: build.example.com
    user: ridge
    port: 22
    root: /srv/ridge/workspace
    python: python3
```

The remote host must contain the configured Python 3.11+ executable. Ridge uses an
ephemeral helper for argument-vector execution, timeout enforcement, and rooted
filesystem operations.
Data [deletion](../concepts/resources.md#deletion) uses the same helper and supports
foreground and background attempts; cancellation does not prove remote work stopped.

Delegated [`data_root` views](../guides/delegation.md#narrow-data-views) are
checked on the remote host when used, including every inherited directory boundary.
Scope creation does not connect or create directories; compute stays resource-wide.

OpenSSH configuration, included files, agents, certificates, proxies, and host
aliases remain active. Optional `identity_file` and `known_hosts_file` paths
are resolved relative to `ridge.yaml`; `executable` may select another SSH
client.

Set up noninteractive authentication and verify the host key before connecting.
Ridge always uses strict host verification and will not prompt for passwords or
accept unknown host keys.

The root must be absolute. OpenSSH uses the remote POSIX login shell only to
bootstrap safely quoted helper source; caller commands remain argument vectors
inside that helper. The helper enforces requested timeouts and rooted filesystem
checks. Stopping the local SSH transport does not prove remote termination.
