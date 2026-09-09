"""Release validation and local Git publication without external side effects."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def release() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/release.py"
    spec = importlib.util.spec_from_file_location("release", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("version", ["v0.1.0", "0.1", "01.1.0", "0.1.0rc1", "1.2.3\n", "a;exit"])
def test_version_rejects_nonfinal_or_ambiguous_input(release: ModuleType, version: str) -> None:
    with pytest.raises(ValueError):
        release.version_tuple(version)


def test_first_release_and_forward_only_versions(release: ModuleType) -> None:
    release.check_version("0.1.0", "0.1.0", [])
    release.check_version("0.1.1", "0.1.0", ["v0.1.0"])
    release.check_version("0.2.0", "0.1.1", ["v0.1.0", "v0.1.1"])
    for version, current, tags in [
        ("0.2.0", "0.1.0", []),
        ("0.1.0", "0.1.0", ["v0.1.0"]),
        ("0.1.1", "0.2.0", ["v0.1.0"]),
        ("0.2.0", "0.1.0", ["v0.3.0"]),
    ]:
        with pytest.raises(ValueError):
            release.check_version(version, current, tags)


def test_extract_only_target_release_notes(release: ModuleType) -> None:
    notes = "# Changelog\n\n## 0.2.0\n\nNew.\n### Fixes\nFixed.\n\n## 0.1.0\n\nOld.\n"
    assert release.release_notes(notes, "0.2.0") == "New.\n### Fixes\nFixed.\n"
    for invalid in ["## 0.2.0\n\n", notes + "\n## 0.2.0\nDuplicate.", "## 0.1.0\nOld."]:
        with pytest.raises(ValueError):
            release.release_notes(invalid, "0.2.0")


@pytest.mark.parametrize(
    "dry_run,fail_push,target",
    [
        (True, False, "0.1.0"),
        (False, False, "0.1.0"),
        (False, True, "0.1.0"),
        (False, False, "0.1.1"),
    ],
)
def test_release_with_disposable_git_remote(
    release: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
    fail_push: bool,
    target: str,
) -> None:
    repository = tmp_path / "checkout"
    remote = tmp_path / "remote.git"
    repository.mkdir()

    def git(*args: str, cwd: Path = repository) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=15
        ).stdout.strip()

    git("init", "--initial-branch=main")
    git("config", "user.name", "Release Test")
    git("config", "user.email", "release@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "tag.gpgsign", "false")
    git("init", "--bare", str(remote))
    git("remote", "add", "origin", str(remote))
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "release-fixture"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'
    )
    (repository / "CHANGELOG.md").write_text(f"## {target}\n\nReviewed release.\n")
    (repository / "uv.lock").write_text(
        'version = 1\nrevision = 3\nrequires-python = ">=3.11"\n\n'
        '[[package]]\nname = "release-fixture"\nversion = "0.1.0"\nsource = { virtual = "." }\n'
    )
    git("add", ".")
    git("commit", "-m", "Initial")
    git("push", "origin", "main")
    original = git("rev-parse", "HEAD")
    previous_tags: list[str] = []
    if target == "0.1.1":
        git("tag", "v0.1.0")
        git("push", "origin", "v0.1.0")
        previous_tags = ["v0.1.0"]
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(release, "ROOT", repository)
    original_run = release.run

    def run(*args: str, capture: bool = False, timeout: int = 120) -> str:
        calls.append(args)
        if args[:3] == ("git", "remote", "get-url"):
            return "git@github.com:vasinov/ridge-core.git"
        if args[:2] == ("git", "push") and fail_push:
            raise subprocess.CalledProcessError(1, args)
        if args[:2] == ("uv", "version"):
            return original_run(
                *args,
                "--offline",
                "--cache-dir",
                str(tmp_path / "cache"),
                capture=capture,
                timeout=timeout,
            )
        if args[0] == "uv":
            return ""
        if args[:3] == ("gh", "run", "list"):
            return json.dumps(
                [{"databaseId": 1, "headBranch": f"v{target}", "url": "https://example.invalid/1"}]
            )
        if args[0] == "gh":
            return ""
        return original_run(*args, capture=capture, timeout=timeout)

    monkeypatch.setattr(release, "run", run)
    if fail_push:
        with pytest.raises(subprocess.CalledProcessError):
            release.publish(target, dry_run)
    else:
        release.publish(target, dry_run)
    if target == "0.1.0":
        assert git("rev-parse", "HEAD") == original  # No empty first-release commit.
    else:
        assert git("rev-parse", "HEAD") != original
        assert set(git("diff", "--name-only", original, "HEAD").splitlines()) == {
            "pyproject.toml",
            "uv.lock",
        }
        assert f'version = "{target}"' in (repository / "uv.lock").read_text()
    assert not git("status", "--porcelain")
    assert git("tag").splitlines() == previous_tags + ([] if dry_run else [f"v{target}"])
    assert git("tag", cwd=remote).splitlines() == previous_tags + (
        [] if dry_run or fail_push else [f"v{target}"]
    )
    if dry_run:
        assert not any(args[0] in {"uv", "gh"} for args in calls)
    elif not fail_push:
        assert (
            "git",
            "push",
            "--atomic",
            "origin",
            "HEAD:refs/heads/main",
            f"refs/tags/v{target}",
        ) in calls
        assert any(args[:3] == ("gh", "run", "watch") for args in calls)
    else:
        assert not any(args[:2] == ("gh", "run") for args in calls)


@pytest.mark.parametrize("failure", ["branch", "dirty", "remote", "notes", "tag"])
def test_preflight_failures_never_mutate(
    release: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (tmp_path / "CHANGELOG.md").write_text("" if failure == "notes" else "## 0.1.0\nFirst.\n")
    monkeypatch.setattr(release, "ROOT", tmp_path)
    allowed: dict[tuple[str, ...], str] = {
        ("git", "branch", "--show-current"): "topic" if failure == "branch" else "main",
        ("git", "status", "--porcelain"): " M x" if failure == "dirty" else "",
        ("git", "remote", "get-url", "origin"): "wrong"
        if failure == "remote"
        else "git@github.com:vasinov/ridge-core.git",
        (
            "git",
            "remote",
            "get-url",
            "--push",
            "--all",
            "origin",
        ): "git@github.com:vasinov/ridge-core.git",
        ("git", "ls-remote", "origin", "refs/heads/main", "refs/tags/v*"): "abc\trefs/heads/main",
        ("git", "merge-base", "--is-ancestor", "abc", "HEAD"): "",
        ("git", "tag", "--list", "v*"): "v0.1.0" if failure == "tag" else "",
    }

    def run(*args: str, capture: bool = False, timeout: int = 120) -> str:
        del capture, timeout
        assert args in allowed, f"Unexpected command after failed preflight: {args}"
        return allowed[args]

    monkeypatch.setattr(release, "run", run)
    with pytest.raises(ValueError):
        release.publish("0.1.0", False)
