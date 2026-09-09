"""Prepare, push, and watch a Ridge release. See docs/development.md."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

REPOSITORY = "vasinov/ridge-core"
ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, capture: bool = False, timeout: int = 120) -> str:
    result = subprocess.run(
        args, cwd=ROOT, check=True, text=True, capture_output=capture, timeout=timeout
    )
    return result.stdout.strip() if capture else ""


def version_tuple(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        raise ValueError("Use a final version such as 0.1.0 (no v prefix or prerelease suffix).")
    return tuple(map(int, value.split(".")))


def release_notes(changelog: str, version: str) -> str:
    version_tuple(version)
    sections = re.findall(
        rf"^## {re.escape(version)}\n(.*?)(?=^## |\Z)", changelog, re.MULTILINE | re.DOTALL
    )
    if len(sections) != 1 or not sections[0].strip():
        raise ValueError(f"CHANGELOG.md needs exactly one nonempty '## {version}' section.")
    return sections[0].strip() + "\n"


def check_version(version: str, current: str, tags: list[str]) -> None:
    requested = version_tuple(version)
    released = [version_tuple(tag[1:]) for tag in tags if re.fullmatch(r"v\d+\.\d+\.\d+", tag)]
    if requested < version_tuple(current) or any(requested <= item for item in released):
        raise ValueError("Version must not decrease and must exceed every existing release tag.")
    if not released and version != "0.1.0":
        raise ValueError("The first release must be 0.1.0.")


def preflight(version: str) -> str:
    version_tuple(version)
    if run("git", "branch", "--show-current", capture=True) != "main":
        raise ValueError("Run the release from the clean, integrated main checkout.")
    if run("git", "status", "--porcelain", capture=True):
        raise ValueError("Commit and integrate reviewed release notes and other changes first.")
    allowed = {f"git@github.com:{REPOSITORY}.git", f"https://github.com/{REPOSITORY}.git"}
    for args in [
        ("remote", "get-url", "origin"),
        ("remote", "get-url", "--push", "--all", "origin"),
    ]:
        if run("git", *args, capture=True) not in allowed:
            raise ValueError(f"origin must fetch and push only github.com/{REPOSITORY}.git.")
    remote = run("git", "ls-remote", "origin", "refs/heads/main", "refs/tags/v*", capture=True)
    refs = dict(line.split()[::-1] for line in remote.splitlines())
    remote_main = refs.get("refs/heads/main")
    if not remote_main:
        raise ValueError("origin has no main branch.")
    run("git", "merge-base", "--is-ancestor", remote_main, "HEAD")
    tags = run("git", "tag", "--list", "v*", capture=True).splitlines()
    tags += [ref.removeprefix("refs/tags/") for ref in refs if ref.startswith("refs/tags/")]
    current = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    check_version(version, current, tags)
    release_notes((ROOT / "CHANGELOG.md").read_text(), version)
    print(f"Release {current} -> {version}; push main and v{version} to {REPOSITORY}.", flush=True)
    print("Outgoing commits:", flush=True)
    run("git", "log", "--oneline", f"{remote_main}..HEAD")
    print(release_notes((ROOT / "CHANGELOG.md").read_text(), version), flush=True)
    return current


def publish(version: str, dry_run: bool) -> None:
    current = preflight(version)
    if dry_run:
        print("Dry run: no files, commits, tags, pushes, or publications changed; checks not run.")
        return
    run("gh", "auth", "status", "--hostname", "github.com")
    if version != current:
        run("uv", "version", version, "--no-sync")
    run("uv", "sync", "--locked", "--group", "docs")
    for command in [
        ("pytest",),
        ("ruff", "check", "."),
        ("ruff", "format", "--check", "."),
        ("pyright",),
        ("mkdocs", "build", "--strict"),
    ]:
        run("uv", "run", "--no-sync", *command, timeout=900)
    # CI builds and verifies the distributions that it will actually publish.
    run("git", "diff", "--check")
    changed = run("git", "diff", "--name-only", capture=True).splitlines()
    if not set(changed) <= {"pyproject.toml", "uv.lock"}:
        raise ValueError("Unexpected changes during release checks; inspect the checkout.")
    if changed:
        run("git", "add", "--", *changed)
        run("git", "diff", "--cached", "--check")
        run("git", "diff", "--cached")
        run("git", "commit", "-m", f"Release {version}")
    if run("git", "status", "--porcelain", capture=True):
        raise ValueError("Checkout changed during checks; inspect it before releasing.")
    tag = f"v{version}"
    run("git", "tag", "-a", tag, "-m", f"Ridge {version}")
    run("git", "push", "--atomic", "origin", "HEAD:refs/heads/main", f"refs/tags/{tag}")
    commit = run("git", "rev-parse", "HEAD", capture=True)
    for _ in range(30):
        runs = json.loads(
            run(
                "gh",
                "run",
                "list",
                "--repo",
                REPOSITORY,
                "--workflow",
                "workflow.yml",
                "--commit",
                commit,
                "--event",
                "push",
                "--json",
                "databaseId,headBranch,url",
                capture=True,
            )
        )
        matching = [item for item in runs if item["headBranch"] == tag]
        if matching:
            print(matching[0]["url"], flush=True)
            run(
                "gh",
                "run",
                "watch",
                str(matching[0]["databaseId"]),
                "--repo",
                REPOSITORY,
                "--exit-status",
                timeout=2400,
            )
            print(
                f"Published https://pypi.org/project/ridge-core/{version}/ and GitHub release {tag}."
            )
            return
        time.sleep(2)
    raise ValueError(
        "Tag pushed, but release run not found yet. Check GitHub Actions; do not retag."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="Explicit final version, e.g. 0.1.0")
    parser.add_argument(
        "--dry-run", action="store_true", help="Read-only preflight and release preview"
    )
    parser.add_argument("--lock-held", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.lock_held:
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/with_integration_lock.py"),
                "--",
                sys.executable,
                str(Path(__file__).resolve()),
                *sys.argv[1:],
                "--lock-held",
            ],
            cwd=ROOT,
            check=False,
        ).returncode
    try:
        publish(args.version, args.dry_run)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Release stopped: {error}", file=sys.stderr)
        print(
            "State is preserved. See docs/development.md for recovery; do not move release tags.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
