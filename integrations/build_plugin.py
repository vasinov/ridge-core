"""Build a workspace-bound local plugin without modifying any client settings."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def build(destination: Path, executable: Path, config: Path, token_file: Path | None) -> Path:
    executable = executable.resolve(strict=True)
    config = config.resolve(strict=True)
    if token_file is not None:
        token_file = token_file.resolve(strict=True)
    if destination.name != "ridge":
        raise ValueError("the destination directory must be named ridge")
    # Never merge with or overwrite an existing plugin or user's configuration.
    destination.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parent
    shutil.copytree(source / "ridge", destination, dirs_exist_ok=True)
    shutil.copytree(source.parent / "skills", destination / "skills", dirs_exist_ok=True)
    arguments = ["--config", str(config)]
    if token_file is not None:
        arguments += ["--scope-token-file", str(token_file)]
    server = {"command": str(executable), "args": arguments}
    (destination / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"ridge": server}}, indent=2) + "\n"
    )
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--scope-token-file", type=Path)
    args = parser.parse_args()
    print(build(args.destination.absolute(), args.executable, args.config, args.scope_token_file))
