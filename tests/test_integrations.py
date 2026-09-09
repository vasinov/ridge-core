"""Integration assets must remain runnable without modifying personal settings."""

import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from ridge import AccessGrant, Operation, RidgeService

ROOT = Path(__file__).resolve().parents[1]


def module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "integrations" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_plugin_build_preserves_skill_and_binding(tmp_path: Path) -> None:
    config = tmp_path / "workspace with spaces.yaml"
    config.write_text("resources: {}")
    token = tmp_path / "private.token"
    token.write_text("secret-not-copied")
    destination = tmp_path / "plugins" / "ridge"
    module("build_plugin").build(destination, Path(sys.executable), config, token)
    server = json.loads((destination / ".mcp.json").read_text())["mcpServers"]["ridge"]
    assert server["command"] == str(Path(sys.executable).resolve())
    assert server["args"] == ["--config", str(config), "--scope-token-file", str(token)]
    assert (destination / "skills/ridge-setup/SKILL.md").read_bytes() == (
        ROOT / "skills/ridge-setup/SKILL.md"
    ).read_bytes()
    for path in destination.rglob("*"):
        if path.is_file():
            assert b"secret-not-copied" not in path.read_bytes()
    with pytest.raises(FileExistsError):
        module("build_plugin").build(destination, Path(sys.executable), config, None)
    assert token.read_text() == "secret-not-copied"


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_agent_launch_uses_process_local_config(client: str) -> None:
    command = module("agent_demo").command(
        client, Path("/workspace/a b.yaml"), Path("/env/ridge-mcp")
    )
    if client == "codex":
        config = tomllib.loads(command[command.index("-c") + 1])["mcp_servers"]["ridge"]
        assert config["env_vars"] == ["RIDGE_SCOPE_TOKEN"]
        assert "--ignore-user-config" in command
        assert "--ephemeral" in command
    else:
        config = json.loads(command[command.index("--mcp-config") + 1])["mcpServers"]["ridge"]
        assert "--strict-mcp-config" in command
        assert "--no-session-persistence" in command
    assert config["command"] == "/env/ridge-mcp"
    assert config["args"] == ["--config", "/workspace/a b.yaml"]
    assert not any("bypass" in arg for arg in command)


def test_agent_demo_refuses_existing_directory(tmp_path: Path) -> None:
    sentinel = tmp_path / "untouched"
    sentinel.write_text("keep")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "integrations/agent_demo.py"),
            str(tmp_path),
            "--client",
            "codex",
            "--ridge-mcp",
            sys.executable,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert sentinel.read_text() == "keep"


def test_agent_host_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = module("agent_demo")
    calls: list[str] = []

    def invoke(
        client: str, config: Path, executable: Path, token: str, prompt: str
    ) -> dict[str, object]:
        del client, executable
        service = RidgeService.from_config(config, scope_token=token)
        calls.append(prompt)
        read = frozenset({Operation.DATA_READ, Operation.DATA_STAT})
        if len(calls) == 1:
            issued = service.create_scope(
                [
                    AccessGrant("inputs", read),
                    AccessGrant("worker", read | {Operation.DATA_WRITE, Operation.COMPUTE_EXEC}),
                    AccessGrant("results", read | {Operation.DATA_WRITE}, data_root="task"),
                ]
            )
            return {"token": issued.token, "scope_id": issued.scope.id}
        if len(calls) == 2:
            service.copy("inputs:values.json", "worker:values.json")
            job = service.submit_execution(
                "worker",
                [
                    sys.executable,
                    "-c",
                    (
                        "import json; from pathlib import Path; "
                        "Path('metrics.json').write_text(json.dumps({'score':sum(json.loads(Path('values.json').read_text()))}))"
                    ),
                ],
                timeout_seconds=10,
            )
            return {"job_id": job.id}
        service.copy("worker:metrics.json", "results:metrics.json")
        return {"published": True}

    monkeypatch.setattr(host, "invoke", invoke)
    workspace = tmp_path / "demo"
    report = host.run(workspace, "codex", Path(sys.executable))
    assert report["score"] == {"score": 10}
    assert len(calls) == 3
    service = RidgeService.from_config(workspace / "ridge.yaml")
    assert all(scope.status == "revoked" for scope in service.list_scopes().scopes)
    assert not service.list_locks()["entries"]
