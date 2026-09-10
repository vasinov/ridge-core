"""Experiment-only MCP access to ordinary Docker/S3 tooling, without Ridge.

The fixture bounds targets to its own paths and containers. This is not a provider
or a general native-tools integration. Workers share its run-wide authority.
"""

import argparse
import json
import subprocess
from pathlib import Path

import boto3
from botocore.config import Config
from mcp.server import MCPServer


def main(config: Path) -> None:
    fixture = json.loads(config.read_text())
    root = Path(fixture["root"])
    s3 = boto3.client(
        "s3",
        region_name=fixture["region"],
        config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 0}),
    )
    if s3.meta.endpoint_url not in {
        "https://s3.amazonaws.com",
        "https://s3.us-east-1.amazonaws.com",
    }:
        raise ValueError("Unexpected AWS endpoint")
    server = MCPServer("native-archive-tools")

    def local(name: str) -> Path:
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("Path outside experiment")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def key(name: str) -> str:
        # All keys are within the new run prefix; no broad cloud access is exposed.
        return fixture["prefix"] + name

    def worker(name: str) -> str:
        return fixture["containers"][name]

    def docker(args: list[str]) -> dict[str, object]:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=120, check=False
        )
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout[-16000:],
            "stderr": result.stderr[-16000:],
        }

    @server.tool()
    def s3_download(object_key: str, local_path: str) -> str:
        """Download a run-relative S3 object to an experiment-local file."""
        s3.download_file(fixture["bucket"], key(object_key), str(local(local_path)))
        return "downloaded"

    @server.tool()
    def s3_upload(local_path: str, object_key: str) -> str:
        """Upload a local artifact to a run-relative S3 key."""
        path = local(local_path)
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("Artifact exceeds experiment bound")
        s3.upload_file(str(path), fixture["bucket"], key(object_key))
        return "uploaded"

    @server.tool()
    def docker_copy_to(local_path: str, worker_name: str, filename: str) -> dict[str, object]:
        """Copy a local file into /work in the selected worker."""
        if Path(filename).name != filename:
            raise ValueError("Use a filename")
        return docker(["cp", str(local(local_path)), f"{worker(worker_name)}:/work/{filename}"])

    @server.tool()
    def docker_copy_from(worker_name: str, filename: str, local_path: str) -> dict[str, object]:
        """Retrieve a worker artifact into the experiment-local directory."""
        if Path(filename).name != filename:
            raise ValueError("Use a filename")
        return docker(["cp", f"{worker(worker_name)}:/work/{filename}", str(local(local_path))])

    @server.tool()
    def docker_execute(worker_name: str, argv: list[str]) -> dict[str, object]:
        """Execute argv in the selected disposable Docker worker, synchronously."""
        return docker(["exec", "-w", "/work", worker(worker_name), *argv])

    @server.tool()
    def read_local(local_path: str) -> str:
        """Read a small local report or recovery manifest."""
        path = local(local_path)
        if path.stat().st_size > 32000:
            raise ValueError("Use file transfer for payloads")
        return path.read_text()

    @server.tool()
    def write_report(local_path: str, content: str) -> str:
        """Write a small comparison report inside the experiment directory."""
        if len(content.encode()) > 32000:
            raise ValueError("Report too large")
        local(local_path).write_text(content)
        return "written"

    server.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    main(parser.parse_args().config)
