"""Connect a LangChain agent to an installed, scope-bound Ridge MCP server."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from langchain.agents import create_agent  # pyright: ignore[reportUnknownVariableType]
from langchain.mcp import MCPAdapter


async def run(executable: Path, config: Path, token_file: Path, model: str, prompt: str) -> str:
    connection = {
        "mcpServers": {
            "ridge": {
                "command": str(executable.resolve(strict=True)),
                "args": [
                    "--config",
                    str(config.resolve(strict=True)),
                    "--scope-token-file",
                    str(token_file.resolve(strict=True)),
                ],
            }
        }
    }
    async with MCPAdapter(connection) as adapter:
        tools = await adapter.list_tools()
        # LangChain leaves its optional context generic unbound when no context is used.
        agent = create_agent(  # pyright: ignore[reportUnknownVariableType]
            model,
            tools=tools,
            system_prompt=(
                "Use Ridge tools for resource work. Inspect your access before acting. "
                "Keep large payloads outside conversation using copy. Use bounded background "
                "execution. Wait for every submitted job before dependent work, check its "
                "status and execution exit_code, and report failures rather than assuming "
                "outputs exist. Paths and compute cwd are resource-relative; omit cwd or use "
                "'.' for the resource root, never '/'. Delegation creates access, not an agent: "
                "only derive child scopes when the host has a secure child-binding mechanism. "
                "Do not print bearer tokens. Return a concise result and artifact references."
            ),
        )
        result = await agent.ainvoke(  # pyright: ignore[reportUnknownMemberType]
            {"messages": [{"role": "user", "content": prompt}]}, config={"recursion_limit": 40}
        )
        return str(result["messages"][-1].content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ridge-mcp", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--scope-token-file", type=Path, required=True)
    parser.add_argument("--model", required=True, help="LangChain provider:model identifier")
    parser.add_argument("prompt")
    args = parser.parse_args()
    print(
        asyncio.run(
            asyncio.wait_for(
                run(args.ridge_mcp, args.config, args.scope_token_file, args.model, args.prompt),
                timeout=180,
            )
        )
    )
