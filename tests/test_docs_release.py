"""Published docs must select released source instead of development main."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def selector() -> ModuleType:
    spec = importlib.util.spec_from_file_location("docs_release", ROOT / "scripts/docs_release.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release(tag: str, **values: Any) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-09-09T00:00:00Z",
        **values,
    }


def test_select_highest_final_across_pages(selector: ModuleType) -> None:
    assert (
        selector.latest_release(
            [
                [release("v0.9.0"), release("v5.0.0", draft=True), release("v0.10.0")],
                [
                    release("v8.0.0", prerelease=True),
                    release("v0.1.0"),
                    release("v9.0.0rc1"),
                    release("v7.0.0", published_at=None),
                    release("v01.2.3"),
                    release("main"),
                ],
            ]
        )
        == "v0.10.0"
    )


def test_no_final_release_never_falls_back_to_main(selector: ModuleType) -> None:
    assert selector.latest_release([[]]) == ""
    assert selector.latest_release([[release("v0.1.0", draft=True)]]) == ""
    with pytest.raises((KeyError, TypeError)):
        selector.latest_release([{"message": "API failure"}])


@pytest.mark.parametrize("published", [True, False])
def test_workflow_resolves_and_rechecks_released_commit(tmp_path: Path, published: bool) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=checkout, text=True, capture_output=True, check=True, timeout=15
        ).stdout.strip()

    git("init", "--initial-branch=main")
    git("config", "user.name", "Docs Test")
    git("config", "user.email", "docs@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "tag.gpgsign", "false")
    (checkout / "scripts").mkdir()
    (checkout / "scripts/docs_release.py").write_bytes(
        (ROOT / "scripts/docs_release.py").read_bytes()
    )
    (checkout / "index.md").write_text("Released documentation")
    git("add", ".")
    git("commit", "-m", "Released source")
    released_sha = git("rev-parse", "HEAD")
    git("tag", "-a", "v0.1.0", "-m", "First release")
    (checkout / "index.md").write_text("Unreleased documentation")
    git("commit", "-am", "Development changes")
    git("init", "--bare", str(tmp_path / "remote.git"))
    git("remote", "add", "origin", str(tmp_path / "remote.git"))
    git("push", "origin", "main", "refs/tags/v0.1.0")

    responses = tmp_path / "releases.json"
    responses.write_text(json.dumps([[release("v0.1.0")] if published else []]))
    commands = tmp_path / "bin"
    commands.mkdir()
    gh = commands / "gh"
    gh.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\n"
        f"print(Path({str(responses)!r}).read_text())\n"
    )
    gh.chmod(0o755)
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    env.update(
        PATH=f"{commands}:{env['PATH']}",
        GH_REPO="fixture/repo",
        GITHUB_OUTPUT=str(output),
        GITHUB_STEP_SUMMARY=str(summary),
        UV_CACHE_DIR=str(tmp_path / "uv-cache"),
        UV_PYTHON=sys.executable,
        UV_OFFLINE="1",
    )
    workflow = yaml.safe_load((ROOT / ".github/workflows/docs.yml").read_text())

    def step(job: str, name: str) -> None:
        source = next(
            item["run"] for item in workflow["jobs"][job]["steps"] if item.get("name") == name
        )
        subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", source],
            cwd=checkout,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

    step("release", "Resolve latest final release")
    if not published:
        assert output.read_text() == "tag=\n"
        assert "keeping the existing" in summary.read_text()
        assert git("branch", "--show-current") == "main"
        return
    assert output.read_text() == f"tag=v0.1.0\nsha={released_sha}\n"
    git("checkout", "--detach", released_sha)
    assert (checkout / "index.md").read_text() == "Released documentation"
    env["BUILT_TAG"] = "v0.1.0"
    output.write_text("")
    step("deploy", "Avoid deploying a superseded release")
    assert output.read_text() == "deploy=true\n"
    responses.write_text(json.dumps([[release("v0.1.0"), release("v0.2.0")]]))
    output.write_text("")
    step("deploy", "Avoid deploying a superseded release")
    assert not output.read_text()
    assert "newer release superseded" in summary.read_text()
