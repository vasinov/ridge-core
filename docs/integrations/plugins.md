# Local plugins

Package Ridge's setup/delegation skill and a workspace-bound MCP connection for
Codex or Claude Code. Ridge must already be installed. A plugin does not start
a hosted service, provision resources, or change workspace permissions.

## Build a workspace-bound bundle

From the Ridge repository at the `vX.Y.Z` tag matching your installed version:

```bash
uv run python integrations/build_plugin.py /absolute/path/to/plugins/ridge \
  --executable /absolute/path/to/environment/bin/ridge-mcp \
  --config /absolute/path/to/ridge.yaml
```

The destination must be new and named `ridge`. The builder includes both client
manifests, the canonical `ridge-setup` skill, and `.mcp.json` with absolute paths.
It does not install the plugin or edit client settings. For scoped access, add
`--scope-token-file /absolute/private/task.token`; the bundle references that file
without copying or reading its token. Protect the bundle's local path metadata.

Build a fresh bundle after updating Ridge's skills or integration assets. Keep
resource YAML, managed state, and token files outside plugin/cache directories.
Do not share a locally bound bundle unchanged: rebuild it for the recipient's
installed executable and workspace.

## Claude Code

Load the bundle for one session:

```bash
claude --plugin-dir /absolute/path/to/plugins/ridge
```

Use `/ridge:ridge-setup` when preparing a workspace or handing off task access.
The bundled MCP server supplies resource tools. Avoid registering a second
direct server for the same connection. See
[Claude plugin loading](https://code.claude.com/docs/en/plugins-reference).

## Codex

Add the bundle through a local plugin marketplace using Codex's plugin tooling
or the plugin-creator workflow, then enable Ridge in a new conversation. The
manifest lives at `.codex-plugin/plugin.json`; the MCP connection is `.mcp.json`.
Keep marketplace registration separate from the build so it never silently
modifies your personal configuration. See
[Codex plugin setup](https://learn.chatgpt.com/docs/build-plugins).

The direct [Codex MCP configuration](clients.md#codex-cli-and-desktop) is also
available without marketplace registration. Install the repository's
`skills/ridge-setup` directory in your client's skill location if you want skill
guidance independently of plugin packaging.
