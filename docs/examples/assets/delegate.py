"""Runnable Ridge delegation plumbing; scripted children, no model service required."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

from ridge import AccessGrant, AuthorizationDeniedError, IssuedScope, Operation, RidgeService

READ = frozenset({Operation.DATA_READ, Operation.DATA_STAT})
WORK = READ | {Operation.DATA_WRITE, Operation.COMPUTE_EXEC}
RESULTS = READ | {Operation.DATA_WRITE}
TERMINAL = {"succeeded", "failed", "cancelled", "lost"}


def child(config: Path, identity: str, variant: str, phase: str) -> None:
    service = RidgeService.from_config(config, scope_token=os.environ["RIDGE_SCOPE_TOKEN"])
    if service.inspect_access()["scope_id"] != identity:
        raise RuntimeError("unexpected child binding")
    if {item.name for item in service.list_resources()} != {
        "inputs",
        "results",
        f"worker-{variant}",
    }:
        raise RuntimeError("unexpected resource visibility")
    if phase == "submit":
        try:
            service.write_data("inputs", "forbidden.txt", b"not allowed")
        except AuthorizationDeniedError:
            pass
        else:
            raise RuntimeError("read-only input accepted a write")
        service.copy("inputs:values.json", f"worker-{variant}:values.json")
        program = (
            "import json; from pathlib import Path; "
            "values=json.loads(Path('values.json').read_text()); "
            f"score={'sum' if variant == 'a' else 'max'}(values); "
            "Path('metrics.json').write_text(json.dumps({'score':score})); print(score)"
        )
        job = service.submit_execution(
            f"worker-{variant}",
            [sys.executable, "-c", program],
            timeout_seconds=10,
            idempotency_key="evaluate",
        )
        print(json.dumps({"job_id": job.id, "scope_id": identity}))
    else:
        service.copy(f"worker-{variant}:metrics.json", "results:metrics.json")
        print(json.dumps({"published": variant, "scope_id": identity}))


def launch(config: Path, token: str, identity: str, variant: str, phase: str) -> dict[str, str]:
    # Tokens travel in the process environment, never argv or a child prompt.
    env = {**os.environ, "RIDGE_SCOPE_TOKEN": token}
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            str(config),
            "--child",
            identity,
            "--variant",
            variant,
            "--phase",
            phase,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


def run_demo(directory: Path) -> dict[str, object]:
    # A new directory is mandatory: never replace an existing user's workspace.
    directory.mkdir(parents=True, exist_ok=False)
    for name in ("inputs", "worker-a", "worker-b", "results/a", "results/b"):
        (directory / name).mkdir(parents=True)
    (directory / "inputs/values.json").write_text("[1, 2, 3, 4]\n")
    grants = {"inputs": READ, "worker-a": WORK, "worker-b": WORK, "results": RESULTS}
    config = directory / "ridge.yaml"
    policy = {name: sorted(op.value for op in ops) for name, ops in grants.items()}
    config.write_text(
        yaml.safe_dump(
            {
                "resources": {name: {"provider": "local", "root": name} for name in grants},
                "permissions": policy,
                "delegation": policy,
                "state": {"directory": ".ridge"},
            }
        )
    )
    operator = RidgeService.from_config(config)
    # One-time operator bootstrap. The parent agent delegates without operator access.
    parent_scope = operator.create_scope(
        [
            AccessGrant(name, READ if name == "results" else frozenset(), ops)
            for name, ops in grants.items()
        ]
    )
    parent = RidgeService.from_config(config, scope_token=parent_scope.token)
    jobs: list[str] = []
    children: list[tuple[str, IssuedScope]] = []
    try:
        for variant in ("a", "b"):
            issued = parent.create_scope(
                [
                    AccessGrant("inputs", READ),
                    AccessGrant(f"worker-{variant}", WORK),
                    AccessGrant("results", RESULTS, data_root=variant),
                ]
            )
            children.append((variant, issued))
            submitted = launch(config, issued.token, issued.scope.id, variant, "submit")
            jobs.append(submitted["job_id"])
        # Submitting child processes have exited; the scoped parent supervises jobs.
        deadline = time.monotonic() + 30
        for job_id in jobs:
            while True:
                job = parent.inspect_job(job_id)
                if job.status.value in TERMINAL:
                    if (
                        job.status.value != "succeeded"
                        or job.result is None
                        or job.result["exit_code"] != 0
                    ):
                        raise RuntimeError(f"evaluation failed: {job_id}: {job.error}")
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("evaluation deadline exceeded")
                time.sleep(0.1)
        scores: dict[str, object] = {}
        for variant, issued in children:
            # A fresh process reconnects with the same scope, then publishes.
            launch(config, issued.token, issued.scope.id, variant, "publish")
            scores[variant] = json.loads(parent.read_data("results", f"{variant}/metrics.json"))
            parent.revoke_scope(issued.scope.id)
            try:
                RidgeService.from_config(config, scope_token=issued.token)
            except AuthorizationDeniedError:
                pass
            else:
                raise RuntimeError("closed child reconnected")
        if scores != {"a": {"score": 10}, "b": {"score": 4}}:
            raise RuntimeError("unexpected results")
        if (directory / "inputs/forbidden.txt").exists():
            raise RuntimeError("denied write had an effect")
        if (directory / "inputs/values.json").read_text() != "[1, 2, 3, 4]\n":
            raise RuntimeError("inputs changed")
        if parent.list_locks()["entries"]:
            raise RuntimeError("outstanding claims")
        return {"scores": scores, "jobs": jobs, "child_scopes_closed": True}
    finally:
        # Closure is not cancellation: stop any known unfinished jobs separately.
        for job_id in jobs:
            if parent.inspect_job(job_id).status.value not in TERMINAL:
                parent.cancel_job(job_id)
        for _, issued in children:
            parent.revoke_scope(issued.scope.id)
        operator.revoke_scope(parent_scope.scope.id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--child")
    parser.add_argument("--variant", choices=("a", "b"), default="a")
    parser.add_argument("--phase", choices=("submit", "publish"), default="submit")
    args = parser.parse_args()
    if args.child:
        child(args.directory.resolve(), args.child, args.variant, args.phase)
    else:
        print(json.dumps(run_demo(args.directory.resolve()), indent=2))
