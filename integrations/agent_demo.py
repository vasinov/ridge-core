"""Run an agent-led Ridge delegation through Codex or Claude Code CLI.

Requires an authenticated client and installed Ridge. Creates a NEW disposable
workspace. Captures model responses in memory because scope issuance contains
bearer handles; only the inspected non-secret report is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from ridge import AccessGrant, AuthorizationDeniedError, Operation, RidgeService

READ = frozenset({Operation.DATA_READ, Operation.DATA_STAT})
WORK = READ | {Operation.DATA_WRITE, Operation.COMPUTE_EXEC}
RESULTS = READ | {Operation.DATA_WRITE}
TERMINAL = {"succeeded", "failed", "cancelled", "lost"}


def command(client: str, config: Path, executable: Path) -> list[str]:
    server = {"command": str(executable), "args": ["--config", str(config)]}
    if client == "claude":
        return [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--mcp-config",
            json.dumps({"mcpServers": {"ridge": server}}),
            "--setting-sources",
            "",
            "--tools",
            "",
            "--allowedTools",
            "mcp__ridge__*",
            "--permission-mode",
            "dontAsk",
            "--max-budget-usd",
            "2",
        ]
    if client != "codex":
        raise ValueError("unknown client")
    return [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-c",
        "mcp_servers.ridge="
        + "{command="
        + json.dumps(str(executable))
        + ",args="
        + json.dumps(server["args"])
        + ',env_vars=["RIDGE_SCOPE_TOKEN"],'
        + 'required=true,default_tools_approval_mode="approve"}',
        "-",
    ]


def invoke(client: str, config: Path, executable: Path, token: str, prompt: str) -> dict[str, Any]:
    result = subprocess.run(
        command(client, config, executable),
        input=prompt,
        text=True,
        capture_output=True,
        env={**os.environ, "RIDGE_SCOPE_TOKEN": token},
        cwd=config.parent,
        timeout=180,
        check=False,
    )
    # Never include a raw transcript or exception stdout in diagnostics: it may contain tokens.
    if result.returncode:
        raise RuntimeError(f"{client} exited {result.returncode}; check client login and MCP setup")
    if client == "claude":
        envelope = json.loads(result.stdout)
        if envelope.get("is_error"):
            raise RuntimeError("Claude reported an unsuccessful run; check login and quota")
        content = envelope["result"]
    else:
        messages = [
            event["item"]["text"]
            for line in result.stdout.splitlines()
            if (event := json.loads(line)).get("type") == "item.completed"
            and event.get("item", {}).get("type") == "agent_message"
        ]
        if not messages:
            raise RuntimeError("Codex returned no final response")
        content = messages[-1]
    content = content.strip()
    if content.startswith("```"):
        content = "\n".join(content.splitlines()[1:-1])
    try:
        return json.loads(content)
    except ValueError:
        raise RuntimeError("agent must return a JSON object; transcript withheld") from None


def run(directory: Path, client: str, executable: Path) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=False)
    for name in ("inputs", "worker", "results/task"):
        (directory / name).mkdir(parents=True)
    original = "[1, 2, 3, 4]\n"
    (directory / "inputs/values.json").write_text(original)
    operations = {"inputs": READ, "worker": WORK, "results": RESULTS}
    policy = {name: sorted(op.value for op in ops) for name, ops in operations.items()}
    config = directory / "ridge.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "resources": {name: {"provider": "local", "root": name} for name in operations},
                "permissions": policy,
                "delegation": policy,
                "state": {"directory": ".ridge"},
            }
        )
    )
    operator = RidgeService.from_config(config)
    issued = operator.create_scope(
        [
            AccessGrant(name, READ if name == "results" else frozenset(), ops)
            for name, ops in operations.items()
        ]
    )
    parent = RidgeService.from_config(config, scope_token=issued.token)
    try:
        print(f"{client}: main agent selecting and issuing task access", file=sys.stderr)
        task = invoke(
            client,
            config,
            executable,
            issued.token,
            "Use only Ridge MCP tools. Inspect your access and resources. Prepare access for a "
            "child to copy inputs:values.json to worker, run a Python sum calculation there, "
            "then publish metrics.json and read it back for verification. The child's results "
            "resource view must be restricted to task/: it must not see or write outside "
            "that directory, which already exists. The worker calculation uses its configured "
            "working root, so its data view must stay aligned with that root: data_root narrows "
            "data operations but does not change compute's working directory. "
            "Choose the minimum grants needed including stat for inline reads. "
            "Inputs must stay read-only. No child "
            "redelegation. Create the child scope now; do not execute its work. Return ONLY "
            'JSON {"scope_id":"...","token":"..."} from actual issuance. This response '
            "goes directly to trusted host code, not a child prompt or saved transcript.",
        )
        child_token, child_id = task["token"], task["scope_id"]
        child = RidgeService.from_config(config, scope_token=child_token)
        access = child.inspect_access()
        if (
            access["scope_id"] != child_id
            or parent.inspect_scope(child_id).parent_id != issued.scope.id
        ):
            raise RuntimeError("unexpected task lineage")
        grants = {g.resource: g for g in parent.inspect_scope(child_id).grants}
        if set(grants) != set(operations) or any(g.delegation for g in grants.values()):
            raise RuntimeError("unexpected child resource or redelegation grants")
        if (
            not {Operation.DATA_READ} <= grants["inputs"].operations <= READ
            or not WORK - {Operation.DATA_STAT} <= grants["worker"].operations <= WORK
            or grants["results"].operations != RESULTS
            or Path(grants["inputs"].data_root or "").parts
            or Path(grants["worker"].data_root or "").parts
            or Path(grants["results"].data_root or "").parts != ("task",)
        ):
            raise RuntimeError("agent did not narrow task access correctly")
        try:
            child.write_data("inputs", "forbidden.txt", b"denied")
        except AuthorizationDeniedError:
            pass
        else:
            raise RuntimeError("read-only input accepted mutation")
        print(f"{client}: child executing with its own binding", file=sys.stderr)
        result = invoke(
            client,
            config,
            executable,
            child_token,
            f"Use only Ridge MCP tools. First inspect_access and verify scope_id {child_id}. "
            "Copy inputs:values.json to worker:values.json. Submit a BACKGROUND compute "
            f"job on worker using interpreter {sys.executable!r} and a short -c program "
            "that reads values.json, sums the numbers, writes metrics.json as "
            '{"score": total}, and prints total. Set timeout_seconds=10 and '
            'idempotency_key="sum". Do not wait or publish yet. Return ONLY JSON '
            '{"job_id":"actual submitted job id"}.',
        )
        job_id = result["job_id"]
        if not isinstance(job_id, str):
            raise TypeError("agent returned an invalid job ID")
        deadline = time.monotonic() + 30
        while (job := parent.inspect_job(job_id)).status.value not in TERMINAL:
            if time.monotonic() > deadline:
                raise TimeoutError("job completion deadline exceeded")
            time.sleep(0.1)
        if job.status.value != "succeeded" or job.result is None or job.result["exit_code"] != 0:
            raise RuntimeError("child calculation did not succeed")
        print(f"{client}: reconnecting child to publish results", file=sys.stderr)
        invoke(
            client,
            config,
            executable,
            child_token,
            f"Use only Ridge MCP tools. Verify inspect_access scope_id {child_id}. "
            "The earlier background calculation succeeded. Copy worker:metrics.json to "
            'results:metrics.json and read it to verify. Return ONLY JSON {"published":true}.',
        )
        score = json.loads(parent.read_data("results", "task/metrics.json"))
        if score != {"score": 10} or (directory / "inputs/values.json").read_text() != original:
            raise RuntimeError("unexpected output or changed inputs")
        parent.revoke_scope(child_id)
        try:
            RidgeService.from_config(config, scope_token=child_token)
        except AuthorizationDeniedError:
            pass
        else:
            raise RuntimeError("revoked child reconnected")
        if parent.list_locks()["entries"] or (directory / "inputs/forbidden.txt").exists():
            raise RuntimeError("outstanding claims or denied mutation side effect")
        return {
            "client": client,
            "score": score,
            "job_id": job_id,
            "agent_issued_scope": child_id,
            "reconnected": True,
            "revoked": True,
        }
    finally:
        # Scope closure is independent of cancellation. Include jobs whose response was lost.
        try:
            for item in parent.list_jobs(limit=200).jobs:
                if item.status.value not in TERMINAL:
                    parent.cancel_job(item.id)
        finally:
            operator.revoke_scope(issued.scope.id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--client", choices=("codex", "claude"), required=True)
    parser.add_argument("--ridge-mcp", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(run(args.directory.resolve(), args.client, args.ridge_mcp.resolve()), indent=2)
    )
