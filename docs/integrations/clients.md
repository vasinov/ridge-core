# Client setup

Install Ridge first, then connect your client to the workspace's YAML file.
Use absolute executable and configuration paths: desktop launch environments
often differ from terminal environments. Keep the workspace state on the Ridge
host, outside replaceable resource trees.

## Codex CLI and desktop

Add this to your Codex configuration (`~/.codex/config.toml`, or
`.codex/config.toml` in a trusted project):

```toml
[mcp_servers.ridge]
command = "/absolute/path/to/environment/bin/ridge-mcp"
args = ["--config", "/absolute/path/to/ridge.yaml"]
required = true
default_tools_approval_mode = "writes"
```

Open a new conversation and ask the agent to inspect its Ridge access and list
resources. In the terminal, `/mcp` shows the connected server. In the desktop
app, use Settings → MCP servers to add or inspect the same local stdio server,
then restart the connection. See [Codex MCP setup](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
for the current configuration and UI.

For a child process, have the host supply `RIDGE_SCOPE_TOKEN` and add
`env_vars = ["RIDGE_SCOPE_TOKEN"]` to that server entry. For desktop-bound access,
append `"--scope-token-file", "/absolute/private/task.token"` to `args` instead
of relying on terminal environment inheritance. A running connection captures
its handle once; restart it to change bindings.

## Claude Code CLI and desktop Code sessions

From your project, register the installed server:

```bash
claude mcp add --transport stdio --scope project ridge -- \
  /absolute/path/to/environment/bin/ridge-mcp --config /absolute/path/to/ridge.yaml
```

Review the project `.mcp.json` and approve the server when prompted. Ask Claude
to inspect its Ridge access and list resources; `/mcp` shows connection status.
Local desktop Code sessions share the CLI's MCP configuration. Open the same
project and inspect its connectors. Keep only one Ridge definition for the
intended binding if you also use Claude Desktop chat.

For scripted children, use process-local `--mcp-config` with
`--strict-mcp-config` and supply the child's `RIDGE_SCOPE_TOKEN` through the host
environment. The [agent handoff example](agents.md) demonstrates this without
editing personal settings. See [Claude Code MCP](https://code.claude.com/docs/en/mcp)
and [desktop configuration](https://code.claude.com/docs/en/desktop#shared-configuration).

## Claude Desktop chat

Open the desktop app's developer configuration and add a local server:

```json
{
  "mcpServers": {
    "ridge": {
      "command": "/absolute/path/to/environment/bin/ridge-mcp",
      "args": ["--config", "/absolute/path/to/ridge.yaml"]
    }
  }
}
```

Merge this entry with existing servers, then restart the app. The local chat
configuration is `claude_desktop_config.json`; it is distinct from the standalone
Claude Code CLI configuration. For scoped access, add `--scope-token-file` and
its absolute protected path to `args`. See the
[desktop MCP configuration relationship](https://code.claude.com/docs/en/desktop#shared-configuration).

## VS Code / GitHub Copilot

Add `.vscode/mcp.json` to the project, preserving existing entries:

```json
{
  "servers": {
    "ridge": {
      "type": "stdio",
      "command": "/absolute/path/to/environment/bin/ridge-mcp",
      "args": ["--config", "/absolute/path/to/ridge.yaml"]
    }
  }
}
```

Use **MCP: List Servers** to start Ridge and inspect its output. Enable its tools
in agent chat, then ask for access/resource discovery. In a remote VS Code window,
the executable and YAML must be available in the environment running the server.
Use a token-file argument for a scoped connection. Follow
[VS Code's MCP guide](https://code.visualstudio.com/docs/agent-customization/mcp-servers)
for server trust and tool selection.

## Other MCP clients

Choose local stdio, set the installed `ridge-mcp` command and `--config` argument,
and verify `inspect_access` before work. A remote resource does not require a
remote MCP server: the local Ridge host reaches it using its configured provider.

For per-child access, use separate connections with distinct bindings. Native
subagents sharing one connection share its Ridge authority; use the
[separate-process handoff](agents.md) when task-specific binding is needed.
