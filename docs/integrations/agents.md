# Agent-led handoff

The parent agent chooses a child's access; the host delivers it to a separate
agent process. Ridge supplies the same resource operations and shared state to
both. No child inventory or operator policy edit is needed.

The repository includes `integrations/agent_demo.py`, a small executable host
for Codex CLI and Claude Code CLI. Unlike the [scripted example](../examples/delegation.md),
the parent agent discovers the workspace and issues the child scope itself. A
child model copies input, submits a bounded calculation, and a fresh child
process reconnects to publish its result.

## Run the example

Install Ridge and authenticate your chosen CLI. From the repository:

```bash
uv run python integrations/agent_demo.py /tmp/ridge-agent-demo \
  --client codex --ridge-mcp /absolute/path/to/environment/bin/ridge-mcp
```

Use `--client claude` for Claude Code. Each invocation requires a new destination;
the script never replaces an existing directory. Model calls use the client's
configured authentication and incur its normal usage. Each agent invocation is
bounded to three minutes; compute is bounded to ten seconds. The fixture is local
and small, with read-only inputs, one worker, and a `results/task` output view.

Expected result: score `10`, a job ID, a child scope ID, and confirmation of
reconnection and revocation. The host checks actual artifacts, scope lineage,
denied input mutation, completed jobs, and outstanding claims. It retains the
workspace for inspection and closes access at the end, including on failure.
The host accepts valid narrower grants on copy-only endpoints and equivalent
local root spellings, but rejects access outside the requested output view
before dispatching the child.

The host captures client responses in memory: issuance includes a bearer token.
The model provider receives that tool result as part of the conversation. The
host passes the token to children through environment binding, never their
prompts or command-line arguments. Do not enable transcript/debug logging for
this example. Normal host/provider data handling still applies.

## Adapt the host

Keep three responsibilities separate:

- **Agent:** discover authority, choose grants and task boundaries, call Ridge.
- **Host:** launch the chosen client, bind its connection, carry handles securely,
  enforce invocation bounds, and collect outcomes.
- **Ridge:** enforce grants, coordinate resources, persist jobs and scope lifecycle.

The sample host supervises completion and revokes access deterministically.
Applications can expose that supervision to the parent agent through ordinary
Ridge tools. Reconnection reuses the same scope, not the same model conversation.
Stopping a task requires cancelling unfinished jobs separately from revocation.

For native subagents, adapt only the launch/binding step when the harness offers
independent MCP configuration. Do not hand a narrowed task prompt to an unchanged
operator connection and call it scoped delegation.
