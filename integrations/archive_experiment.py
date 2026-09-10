"""Bounded local/Docker/AWS archive experiment; see docs/integrations/archive-experiment.md.

Evidence and scope handles stay in the supplied private scratch directory. The
native mode uses an experiment-only Docker/Boto3 MCP adapter, not Ridge providers.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import lzma
import os
import random
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import boto3
import yaml
from agent_demo import command
from botocore.config import Config
from botocore.exceptions import ClientError

from ridge import AccessGrant, AuthorizationDeniedError, Operation, RidgeService
from ridge.errors import ResourceNotFoundError

HERE = Path(__file__).resolve().parent
READ = frozenset({Operation.DATA_READ, Operation.DATA_STAT})
WORK = READ | {Operation.DATA_WRITE, Operation.COMPUTE_EXEC}
WRITE = READ | {Operation.DATA_WRITE}
TERMINAL = {"succeeded", "failed", "cancelled", "lost"}


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


def aws():
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 0}),
    )
    if client.meta.endpoint_url not in {
        "https://s3.amazonaws.com",
        "https://s3.us-east-1.amazonaws.com",
    }:
        raise ValueError("Unexpected AWS endpoint")
    return client


def shell(args: list[str]) -> str:
    return subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=120
    ).stdout.strip()


def setup(root: Path, bucket: str) -> None:
    root.mkdir(parents=True, exist_ok=False)
    os.chmod(root, 0o700)
    run_id = "archive-" + time.strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8]
    fixture = {
        "root": str(root),
        "bucket": bucket,
        "region": "us-east-1",
        "prefix": f"smoke-tests/{run_id}/",
        "run_id": run_id,
        "containers": {},
    }
    save(root / "fixture.json", fixture)
    s3 = aws()
    assert not s3.list_objects_v2(Bucket=bucket, Prefix=fixture["prefix"]).get("Contents")
    rng = random.Random(20260910)
    source = bytearray()
    index = 0
    while len(source) < 12 * 1024 * 1024:
        row = {
            "request": index,
            "service": ["api", "queue", "search"][index % 3],
            "status": 500 if index % 37 == 0 else 200,
            "route": f"/items/{index % 100}",
            "elapsed_ms": rng.randrange(1, 900),
            "trace": f"{rng.getrandbits(128):032x}",
            "message": "request completed successfully" if index % 37 else "retry later",
        }
        source.extend(json.dumps(row, separators=(",", ":")).encode() + b"\n")
        index += 1
    (root / "logs.jsonl").write_bytes(source)
    fixture["input_sha256"] = hashlib.sha256(source).hexdigest()
    fixture["input_bytes"] = len(source)
    s3.upload_file(str(root / "logs.jsonl"), bucket, fixture["prefix"] + "inputs/logs.jsonl")
    for algorithm in ("gzip", "lzma"):
        container = run_id + "-" + algorithm
        shell(
            [
                "docker",
                "run",
                "-d",
                "--init",
                "--name",
                container,
                "--label",
                f"ridge.experiment={run_id}",
                "--memory",
                "512m",
                "--cpus",
                "1",
                "--network",
                "none",
                "python:3.13-slim",
                "sleep",
                "7200",
            ]
        )
        fixture["containers"][algorithm] = container
        save(root / "fixture.json", fixture)
        shell(["docker", "exec", container, "mkdir", "/work"])
    save(root / "fixture.json", fixture)
    print(json.dumps({"setup": "ready", "input_bytes": len(source), "workers": 2}))


def invoke(
    root: Path,
    label: str,
    config: Path,
    prompt: str,
    token: str | None = None,
    native: bool = False,
) -> dict:
    if token == "":
        raise ValueError("An explicit child binding must not be empty")
    executable = root / "native-mcp" if native else Path(sys.executable).parent / "ridge-mcp"
    args = command("codex", config, executable)
    environment = {key: value for key, value in os.environ.items() if key != "RIDGE_SCOPE_TOKEN"}
    if token:
        environment["RIDGE_SCOPE_TOKEN"] = token
    started = time.monotonic()
    result = subprocess.run(
        args,
        input=prompt + "\nReturn only a JSON object. Do not include credentials or tokens "
        "unless explicitly issuing scope handles to this trusted host.",
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
        env=environment,
        cwd=root,
    )
    events = []
    for line in result.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    calls = []
    messages = []
    for event in events:
        item = event.get("item", {})
        if event.get("type") == "item.completed":
            if item.get("type") == "agent_message":
                messages.append(item.get("text", ""))
            elif item.get("type") == "mcp_tool_call":
                call = {
                    "tool": item.get("tool"),
                    "status": item.get("status"),
                    "error": bool(item.get("error")) or item.get("status") == "failed",
                }
                if item.get("tool") in {
                    "copy",
                    "execute",
                    "write_data",
                    "delete_data",
                    "inspect_job",
                }:
                    arguments = item.get("arguments", {})
                    if isinstance(arguments, dict):
                        call["arguments"] = {
                            key: value
                            for key, value in arguments.items()
                            if key
                            in {
                                "resource",
                                "path",
                                "source",
                                "destination",
                                "background",
                                "idempotency_key",
                                "job_id",
                            }
                        }
                    if item.get("status") == "failed":
                        # These tools return operation diagnostics, never newly issued handles.
                        diagnostic = json.dumps(item.get("result") or item.get("error"))
                        if token:
                            diagnostic = diagnostic.replace(token, "[redacted]")
                        lock_token = (
                            arguments.get("lock_token") if isinstance(arguments, dict) else None
                        )
                        if isinstance(lock_token, str) and lock_token:
                            diagnostic = diagnostic.replace(lock_token, "[redacted]")
                        call["diagnostic"] = diagnostic[:2000]
                calls.append(call)
    # Raw transcripts and scope-issuance responses must never be persisted.
    save(
        root / f"{label}.trace.json",
        {
            "seconds": round(time.monotonic() - started, 3),
            "returncode": result.returncode,
            "calls": calls,
            "usage": [e.get("usage") for e in events if e.get("usage")],
            "event_types": sorted({e.get("type", "") for e in events}),
        },
    )
    if result.returncode or not messages:
        raise RuntimeError(f"Agent {label} failed; raw transcript withheld")
    content = messages[-1].strip()
    if content.startswith("```"):
        content = "\n".join(content.splitlines()[1:-1])
    try:
        answer = json.loads(content)
    except ValueError:
        raise RuntimeError(f"Agent {label} did not return JSON; raw transcript withheld") from None
    print(
        json.dumps(
            {
                "agent": label,
                "seconds": round(time.monotonic() - started, 1),
                "tool_calls": len(calls),
            }
        ),
        flush=True,
    )
    return answer


def configure(root: Path, mode: str) -> tuple[Path, dict]:
    fixture = json.loads((root / "fixture.json").read_text())
    directory = root / mode
    directory.mkdir(exist_ok=False)
    (directory / "code").mkdir()
    (directory / "reports").mkdir()
    shutil.copyfile(HERE / "archive_benchmark.py", directory / "code/benchmark.py")
    resources = {
        "code": {"provider": "local", "root": str(directory / "code")},
        "reports": {"provider": "local", "root": str(directory / "reports")},
        "inputs": {
            "provider": "s3",
            "bucket": fixture["bucket"],
            "region": "us-east-1",
            "prefix": fixture["prefix"] + "inputs",
        },
        "results": {
            "provider": "s3",
            "bucket": fixture["bucket"],
            "region": "us-east-1",
            "prefix": fixture["prefix"] + mode,
        },
    }
    permissions = {"code": READ, "reports": WRITE, "inputs": READ, "results": WRITE}
    for name, container in fixture["containers"].items():
        # Reuse the same two isolated workers, but empty only the known run-owned files.
        shell(
            [
                "docker",
                "exec",
                container,
                "rm",
                "-f",
                "/work/logs.jsonl",
                "/work/benchmark.py",
                "/work/metrics.json",
                "/work/archive.bin",
            ]
        )
        resources[name] = {
            "provider": "docker",
            "container": container,
            "root": "/work",
            "python": "python3",
        }
        permissions[name] = WORK
    policy = {name: sorted(op.value for op in ops) for name, ops in permissions.items()}
    config = directory / "ridge.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "resources": resources,
                "permissions": policy,
                "delegation": policy,
                "state": {"directory": str(directory / "state")},
            }
        )
    )
    save(directory / "native.json", {**fixture, "root": str(directory)})
    # A short launcher lets both variants use the existing, independently bound CLI host.
    import shlex

    launcher = directory / "native-mcp"
    launcher.write_text(
        "#!/bin/sh\nexec "
        + shlex.quote(sys.executable)
        + " "
        + shlex.quote(str(HERE / "archive_native.py"))
        + ' "$@"\n'
    )
    launcher.chmod(0o700)
    return config, permissions


def wait_job(service: RidgeService, job_id: str) -> dict:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = service.inspect_job(job_id)
        if job.status.value in TERMINAL:
            return asdict(job)
        time.sleep(0.2)
    raise TimeoutError("Worker did not settle in two minutes")


def run(root: Path, mode: str, resume: bool = False) -> None:
    started = time.monotonic()
    fixture = json.loads((root / "fixture.json").read_text())
    if resume:
        if mode not in {"shared", "scoped"}:
            raise ValueError("Resume requires a Ridge trial with retained scope/job records")
        config = root / mode / "ridge.yaml"
        permissions = {}
    else:
        config, permissions = configure(root, mode)
    directory = config.parent
    native = mode == "native"
    service = RidgeService.from_config(config)
    tokens = dict.fromkeys(("gzip", "lzma", "collector", "parent"))
    scope_ids = {}
    if mode == "scoped" and resume:
        tokens = json.loads((directory / "handles.json").read_text())
        scope_ids = json.loads((directory / "scope-ids.json").read_text())
        for name, token in tokens.items():
            assert (
                token
                and RidgeService.from_config(config, scope_token=token).inspect_access()["scope_id"]
                == scope_ids[name]
            )
    elif mode == "scoped":
        parent = service.create_scope(
            [AccessGrant(name, ops, ops) for name, ops in permissions.items()]
        )
        tokens["parent"] = parent.token
        scope_ids["parent"] = parent.scope.id
        chosen = invoke(
            directory,
            "issue",
            config,
            "Use only Ridge MCP. Inspect access and resources, then issue three child scopes. "
            "Workers gzip and lzma each get: code and inputs data.read/data.stat; their own "
            "same-named worker data.read/data.stat/data.write/compute.exec; "
            "results data.read/data.stat/data.write with data_root equal to their algorithm. "
            "The collector gets results data.read/data.stat without a narrower root and reports "
            "data.read/data.stat/data.write. No child may redelegate. No extra resources or "
            'operations. Return {"gzip":{"id":"...","token":"..."}, '
            '"lzma":{"id":"...","token":"..."},"collector":{"id":"...","token":"..."}}.',
            parent.token,
        )
        for name in ("gzip", "lzma", "collector"):
            identity, token = chosen[name]["id"], chosen[name]["token"]
            info = service.inspect_scope(identity)
            expected = (
                {"results": (READ, None), "reports": (WRITE, None)}
                if name == "collector"
                else {
                    "code": (READ, None),
                    "inputs": (READ, None),
                    name: (WORK, None),
                    "results": (WRITE, name),
                }
            )
            actual = {g.resource: (g.operations, g.data_root) for g in info.grants}
            assert info.parent_id == parent.scope.id and actual == expected
            assert not any(g.delegation for g in info.grants)
            assert (
                RidgeService.from_config(config, scope_token=token).inspect_access()["scope_id"]
                == identity
            )
            tokens[name], scope_ids[name] = token, identity
        handle_file = directory / "handles.json"
        fd = os.open(handle_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(tokens, stream)
    save(directory / "scope-ids.json", scope_ids)
    receipts = {
        name: json.loads(path.read_text())
        for name in ("gzip", "lzma")
        if (path := directory / f"{name}-receipt.json").exists()
    }

    def prepare(name: str) -> tuple[str, dict]:
        key = f"benchmark-{name}" if mode == "shared" else "benchmark"
        common = (
            f"Your assignment: evaluate {name} compression of logs.jsonl with the supplied "
            "unchanged benchmark.py. Use only MCP tools. Keep inputs and code unchanged. "
            f"Your dedicated Docker worker is {name}. Do not publish yet. "
        )
        if native:
            prompt = common + (
                f"Download S3 inputs/logs.jsonl to staging/{name}/logs.jsonl. Copy that file and "
                f"local code/benchmark.py to worker {name} as logs.jsonl and benchmark.py. "
                f'Execute ["python3","benchmark.py","{name}"] there. '
                'Return {"exit_code":actual_code,"completed":true}.'
            )
        else:
            prompt = common + (
                f"Copy inputs:logs.jsonl to {name}:logs.jsonl and code:benchmark.py to "
                f'{name}:benchmark.py. Execute ["python3","benchmark.py","{name}"] on {name}, '
                f'background=true, timeout_seconds=90, idempotency_key="{key}". '
                'Return {"job_id":"actual ID"} immediately after admission.'
            )
        result = invoke(
            directory,
            name + "-prepare",
            directory / "native.json" if native else config,
            prompt,
            tokens[name],
            native,
        )
        if not native:
            result["job"] = wait_job(service, result["job_id"])
            assert result["job"]["result"]["exit_code"] == 0
        else:
            assert result["exit_code"] == 0
        save(directory / f"{name}-receipt.json", result)
        return name, result

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts.update(
            pool.map(prepare, [name for name in ("gzip", "lzma") if name not in receipts])
        )
    save(directory / "receipts.json", receipts)

    def publish(name: str) -> tuple[str, dict]:
        receipt = directory / f"{name}-published.json"
        if receipt.exists():
            return name, json.loads(receipt.read_text())
        common = (
            "You are a fresh process continuing an archive assignment. Use only MCP tools. "
            f"The previous process computed {name} on worker {name}. "
        )
        if native:
            prompt = common + (
                f"The retained execution receipt is {json.dumps(receipts[name])}. "
                f"Retrieve metrics.json and archive.bin from worker {name} into staging/{name}/. "
                f"Upload them to S3 keys native/{name}/metrics.json and native/{name}/archive.bin. "
                f'Read local staging/{name}/metrics.json. Return {{"published":true}}.'
            )
        else:
            prefix = "" if mode == "scoped" else name + "/"
            prompt = common + (
                f"Inspect job {receipts[name]['job_id']}; require actual exit_code zero. "
                f"Copy {name}:metrics.json to results:{prefix}metrics.json and {name}:archive.bin "
                f"to results:{prefix}archive.bin. Read back results:{prefix}metrics.json. "
                'Return {"published":true}.'
            )
        result = invoke(
            directory,
            name + "-publish",
            directory / "native.json" if native else config,
            prompt,
            tokens[name],
            native,
        )
        save(receipt, result)
        return name, result

    with ThreadPoolExecutor(max_workers=2) as pool:
        published = dict(pool.map(publish, ("gzip", "lzma")))
    assert all(value.get("published") for value in published.values())
    manifest = {
        "assignment": "Compare gzip and lzma archives",
        "scope_ids": scope_ids,
        "receipts": {
            name: {k: v for k, v in item.items() if k != "job"} for name, item in receipts.items()
        },
    }
    if mode == "scoped":
        gzip_service = RidgeService.from_config(config, scope_token=tokens["gzip"])
        failed = gzip_service.submit_execution(
            "gzip",
            ["python3", "-c", "raise SystemExit(7)"],
            timeout_seconds=10,
            idempotency_key="nonzero-probe",
        )
        manifest["nonzero_probe"] = {"job_id": failed.id, "scope_id": scope_ids["gzip"]}
        save(directory / "nonzero-job.json", wait_job(service, failed.id))
        probes = {}
        for label, action in {
            "input_write": lambda: gzip_service.write_data("inputs", "forbidden.txt", b"probe"),
            "sibling_job": lambda: gzip_service.inspect_job(receipts["lzma"]["job_id"]),
            "sibling_worker": lambda: gzip_service.read_data("lzma", "metrics.json"),
        }.items():
            try:
                action()
            except (AuthorizationDeniedError, ResourceNotFoundError) as error:
                probes[label] = type(error).__name__
            else:
                raise AssertionError(f"Probe unexpectedly permitted: {label}")
        # An S3 ../ component stays an opaque key inside the caller's own prefix.
        gzip_service.write_data("results", "../lzma/probe.txt", b"probe")
        s3 = aws()
        exact = fixture["prefix"] + "scoped/gzip/../lzma/probe.txt"
        assert s3.get_object(Bucket=fixture["bucket"], Key=exact)["Body"].read() == b"probe"
        assert not s3.list_objects_v2(
            Bucket=fixture["bucket"], Prefix=fixture["prefix"] + "scoped/lzma/probe.txt"
        ).get("Contents")
        probes["sibling_output_escape"] = "literal key in own prefix; sibling unchanged"
        save(directory / "probes.json", probes)
    save(directory / "reports/manifest.json", manifest)
    if native:
        recovery_prompt = (
            "Use only MCP. You are a fresh parent without prior conversation. "
            "Read reports/manifest.json and fetch both native/gzip/metrics.json and "
            "native/lzma/metrics.json from S3 into recovery/. Inspect the metrics. "
            "Report which assignment produced which outcome, what can be verified, "
            "and any missing ownership or execution information. Do not rerun work."
        )
    else:
        recovery_prompt = (
            "Use only Ridge MCP. You are a fresh parent without prior conversation. "
            "Read reports:manifest.json. List jobs and inspect the retained job IDs "
            "(including any probe). Read results:gzip/metrics.json and "
            "results:lzma/metrics.json. Report assignment ownership, actual exit codes, "
            "whether outputs are usable, and information missing from job summaries. "
            "Distinguish operation success from command success. Do not rerun work."
        )
    recovery = invoke(
        directory,
        "fresh-parent",
        directory / "native.json" if native else config,
        recovery_prompt,
        tokens["parent"],
        native,
    )
    save(directory / "recovery.json", recovery)
    collector_prompt = (
        "Use only MCP data tools; do not execute commands. Compare the two metrics "
        "reports and recommend gzip or lzma for fast archival versus smallest storage. "
        "Quote actual bytes, ratios, timing samples, and hash verification. Write a "
        'small Markdown report. Return {"report_written":true}. '
    )
    if native:
        collector_prompt += (
            "Download native/gzip/metrics.json and native/lzma/metrics.json from S3 "
            "to collector/gzip.json and collector/lzma.json, read them, then write "
            "reports/comparison.md."
        )
    else:
        collector_prompt += (
            "Read results:gzip/metrics.json and results:lzma/metrics.json; "
            "write reports:comparison.md."
        )
    invoke(
        directory,
        "collector",
        directory / "native.json" if native else config,
        collector_prompt,
        tokens["collector"],
        native,
    )
    assert (directory / "reports/comparison.md").stat().st_size > 100
    verified = {}
    s3 = aws()
    for name in ("gzip", "lzma"):
        prefix = fixture["prefix"] + mode + "/" + name + "/"
        metadata = json.loads(
            s3.get_object(Bucket=fixture["bucket"], Key=prefix + "metrics.json")["Body"].read()
        )
        packed = s3.get_object(Bucket=fixture["bucket"], Key=prefix + "archive.bin")["Body"].read()
        unpacked = (gzip.decompress if name == "gzip" else lzma.decompress)(packed)
        assert hashlib.sha256(unpacked).hexdigest() == fixture["input_sha256"]
        assert hashlib.sha256(packed).hexdigest() == metadata["archive_sha256"]
        assert metadata["input_sha256"] == fixture["input_sha256"]
        verified[name] = metadata
        (directory / f"{name}-archive.bin").write_bytes(packed)
    source = s3.get_object(Bucket=fixture["bucket"], Key=fixture["prefix"] + "inputs/logs.jsonl")[
        "Body"
    ].read()
    assert hashlib.sha256(source).hexdigest() == fixture["input_sha256"]
    assert not service.list_locks()["entries"]
    if mode == "scoped":
        service.revoke_scope(scope_ids["parent"])
        for token in tokens.values():
            try:
                RidgeService.from_config(config, scope_token=token)
            except AuthorizationDeniedError:
                pass
            else:
                raise AssertionError("Revoked scope reconnected")
        (directory / "handles.json").unlink()
    save(
        directory / "outcome.json",
        {
            "mode": mode,
            "seconds": time.monotonic() - started,
            "verified": verified,
            "input_unchanged": True,
            "remaining_claims": 0,
        },
    )
    print(json.dumps({"mode": mode, "verified": True}), flush=True)


def cleanup(root: Path) -> None:
    fixture = json.loads((root / "fixture.json").read_text())
    s3 = aws()
    bucket, prefix = fixture["bucket"], fixture["prefix"]
    report = {
        "removed_containers": [],
        "deleted_keys": [],
        "retained_versions": [],
        "inspection_gaps": [],
    }
    for mode in ("shared", "scoped"):
        config = root / mode / "ridge.yaml"
        if config.exists():
            service = RidgeService.from_config(config)
            for job in service.list_jobs(limit=200).jobs:
                if job.status.value not in TERMINAL:
                    service.cancel_job(job.id)
            if (
                any(job.status.value not in TERMINAL for job in service.list_jobs(limit=200).jobs)
                or service.list_locks()["entries"]
            ):
                raise RuntimeError("Unsettled work remains; inspect it before artifact cleanup")
            for scope in service.list_scopes(limit=100).scopes:
                if scope.parent_id is None and scope.status == "active":
                    service.revoke_scope(scope.id)
            (root / mode / "handles.json").unlink(missing_ok=True)
    uploads = s3.list_multipart_uploads(Bucket=bucket, Prefix=prefix).get("Uploads", [])
    report["multipart_before_cleanup"] = len(uploads)
    for upload in uploads:
        s3.abort_multipart_upload(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])
    objects = s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
    for item in objects:
        metadata = s3.head_object(Bucket=bucket, Key=item["Key"])
        version = metadata.get("VersionId")
        if version:
            try:
                s3.delete_object(Bucket=bucket, Key=item["Key"], VersionId=version)
            except ClientError as error:
                if error.response["Error"]["Code"] != "AccessDenied":
                    raise
                report["retained_versions"].append({"key": item["Key"], "version": version})
                s3.delete_object(Bucket=bucket, Key=item["Key"])
        else:
            s3.delete_object(Bucket=bucket, Key=item["Key"])
        report["deleted_keys"].append(item["Key"])
    try:
        versions = s3.list_object_versions(Bucket=bucket, Prefix=prefix)
        report["version_inventory"] = {
            name: versions.get(name, []) for name in ("Versions", "DeleteMarkers")
        }
    except ClientError as error:
        report["inspection_gaps"].append("version inventory: " + type(error).__name__)
    assert not s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents")
    assert not s3.list_multipart_uploads(Bucket=bucket, Prefix=prefix).get("Uploads")
    for container in fixture["containers"].values():
        label = shell(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "ridge.experiment"}}',
                container,
            ]
        )
        assert label == fixture["run_id"]
        shell(["docker", "rm", "-f", container])
        report["removed_containers"].append(container)
    save(root / "cleanup.json", report)
    print(
        json.dumps(
            {
                "cleanup": "done",
                "deleted_keys": len(objects),
                "known_retained_versions": len(report["retained_versions"]),
                "inspection_gaps": report["inspection_gaps"],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "native", "shared", "scoped", "cleanup"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--bucket")
    parser.add_argument(
        "--resume", action="store_true", help="Resume a Ridge trial with retained receipts"
    )
    args = parser.parse_args()
    root = args.directory.resolve()
    if args.action == "setup":
        if not args.bucket:
            parser.error("setup requires --bucket pointing to an authorized disposable S3 target")
        setup(root, args.bucket)
    elif args.action == "cleanup":
        cleanup(root)
    else:
        run(root, args.action, args.resume)
