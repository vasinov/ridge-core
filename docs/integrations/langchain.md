# LangChain

Give a LangChain agent Ridge tools through its existing MCP adapter. Resource
definitions, permissions, delegation, and jobs remain in Ridge; no parallel tool
implementation or framework dependency is added to Ridge's base package.

The repository's `integrations/langchain_agent.py` connects to an installed
`ridge-mcp` using an explicit task token file. Install the framework and your
model provider in a separate environment:

```bash
uv venv .venv-langchain
uv pip install --python .venv-langchain/bin/python 'langchain[mcp]>=1.4,<2' 'langchain-openai>=1,<2'
```

Use ambient provider authentication. Have the workspace owner or authorized
parent create task access and place only the returned token in a protected file.
Then run:

```bash
.venv-langchain/bin/python integrations/langchain_agent.py \
  --ridge-mcp /absolute/path/to/ridge/environment/bin/ridge-mcp \
  --config /absolute/path/to/ridge.yaml \
  --scope-token-file /absolute/private/task.token \
  --model openai:YOUR_MODEL \
  'Inspect my access, copy inputs:values.json to worker:values.json, calculate its sum, and publish the result.'
```

Choose a model available to your account. Other providers need their LangChain
provider package and credentials. The sample bounds the agent run to three
minutes and forty graph steps; your model provider bills normal usage.

Resource paths and compute working directories are relative to the selected
resource. Use `.` or omit `cwd` for its root, not `/`. Before dependent work,
inspect every submitted job and its execution exit code; submission alone does
not mean its outputs are ready.

Use one adapter/connection per child binding. Do not change a global environment
variable to switch concurrent agents' access. A child can reconnect using the
same token file while its scope remains active; replacing file contents does
not rebind a running server. The host remains responsible for child spawning,
scope closure, and cancelling unfinished jobs. Avoid model tracing that records
scope issuance unless your application explicitly permits storing bearer handles.

See [LangChain's MCP adapter](https://docs.langchain.com/oss/python/langchain/mcp)
for framework connection details and [delegating work](../guides/delegation.md)
for the shared lifecycle. Add LangGraph-specific orchestration only when the
application needs it; Ridge does not require a graph wrapper.
