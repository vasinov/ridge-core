# SSH resources

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

The remote host must contain the configured Python 3 executable. Ridge uses an
ephemeral helper for argument-vector execution, timeout enforcement, and rooted
filesystem operations.

OpenSSH configuration, included files, agents, certificates, proxies, and host
aliases remain active. Optional `identity_file` and `known_hosts_file` paths
are resolved relative to `ridge.yaml`; `executable` may select another SSH
client.

Authentication is noninteractive and host verification is always strict.
Ridge does not provision accounts, manage keys, prompt for passwords, or accept
unknown host keys.

The root must be absolute. OpenSSH uses the remote POSIX login shell only to
bootstrap safely quoted helper source; caller commands remain argument vectors
inside that helper. The helper enforces requested timeouts and rooted filesystem
checks. Stopping the local SSH transport does not prove remote termination.
