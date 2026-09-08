"""Run a bounded, read-only managed MCP workflow against an existing inventory."""

from __future__ import annotations

import argparse
import asyncio
import sys

from mcp import Client, StdioServerParameters

from ridge import JobScope, ManagedMCPSession, Operation


async def run(config: str, resource: str, path: str) -> None:
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "ridge.mcp", "--config", config]
    )
    async with (
        Client(server) as client,
        ManagedMCPSession(
            client, [JobScope(resource, Operation.DATA_STAT)], lease_seconds=1
        ) as session,
    ):
        for index in range(3):
            result = await session.call_tool("stat_data", {"resource": resource, "path": path})
            if result.is_error:
                raise RuntimeError("stat_data failed; check the resource, path, and grants")
            print(result.structured_content)
            if index < 2:
                # Substitute an asynchronous model turn here. Do not block the event loop.
                await asyncio.sleep(1.2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--path", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.config, args.resource, args.path))


if __name__ == "__main__":
    main()
