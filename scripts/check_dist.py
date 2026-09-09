"""Verify both release distributions in fresh environments outside the checkout."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

SMOKE = """
import asyncio
import importlib.metadata
import sys
from pathlib import Path
import ridge
from mcp import Client, StdioServerParameters
from ridge.backends._source import HELPER_SOURCE, TRANSFER_HELPER_SOURCE

assert importlib.metadata.version("ridge-core") == sys.argv[1]
assert Path(ridge.__file__).is_relative_to(Path(sys.prefix))
compile(HELPER_SOURCE, "operations", "exec")
compile(TRANSFER_HELPER_SOURCE, "transfer", "exec")
assert importlib.resources.files("ridge").joinpath("py.typed").is_file()

async def smoke():
    params = StdioServerParameters(command=str(Path(sys.executable).with_name("ridge-mcp")),
                                  args=["--config", str(Path("ridge.yaml").resolve())])
    async with asyncio.timeout(20):
        async with Client(params) as client:
            assert "list_resources" in {tool.name for tool in (await client.list_tools()).tools}
            result = await client.call_tool("read_data", {"resource": "local", "path": "input"})
            assert not result.is_error, result
            assert result.structured_content["content"] == "release smoke" + chr(10), result
asyncio.run(smoke())
"""


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve()
    artifacts = [
        dist / f"ridge_core-{version}-py3-none-any.whl",
        dist / f"ridge_core-{version}.tar.gz",
    ]
    if set(dist.iterdir()) - {dist / ".gitignore"} != set(artifacts):
        raise ValueError("Expected exactly the current version's wheel and source distribution.")
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
            "RIDGE_CONFIG",
            "RIDGE_SCOPE_TOKEN",
            "RIDGE_SCOPE_TOKEN_FILE",
            "UV_INDEX",
            "UV_DEFAULT_INDEX",
            "UV_INDEX_URL",
            "UV_EXTRA_INDEX_URL",
            "UV_FIND_LINKS",
        }
    }
    for artifact in artifacts:
        with tempfile.TemporaryDirectory(prefix="ridge-dist-") as directory:
            scratch = Path(directory)
            python = scratch / "venv/bin/python"

            def run(*command: str, cwd: Path = scratch) -> None:
                subprocess.run(command, cwd=cwd, env=env, check=True, timeout=180)

            run("uv", "venv", "--no-config", "--python", sys.executable, str(scratch / "venv"))
            run(
                "uv",
                "pip",
                "install",
                "--no-config",
                "--python",
                str(python),
                "--default-index",
                "https://pypi.org/simple",
                str(artifact),
            )
            (scratch / "data").mkdir()
            (scratch / "data/input").write_text("release smoke\n")
            (scratch / "ridge.yaml").write_text(
                "resources: {local: {provider: local, root: data}}\nstate: {directory: state}\n"
            )
            run(str(python.with_name("ridge")), "config", "validate")
            run(str(python.with_name("ridge")), "copy", "local:input", "local:output")
            assert (scratch / "data/output").read_bytes() == (scratch / "data/input").read_bytes()
            run(str(python), "-I", "-c", SMOKE, version)
            print(
                f"Verified installed CLI, MCP, API and helper sources: {artifact.name}", flush=True
            )


if __name__ == "__main__":
    main()
