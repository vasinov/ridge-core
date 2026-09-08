from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


def test_integration_lock_shared_by_worktrees_and_survives_wrapper_death(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    linked = tmp_path / "linked"
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", "linked", str(linked)],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    wrapper = Path(__file__).resolve().parents[1] / "scripts" / "with_integration_lock.py"
    ready = tmp_path / "ready"
    done = tmp_path / "done"
    second = tmp_path / "second"
    program = (
        "import sys,time; from pathlib import Path; "
        "Path(sys.argv[1]).touch(); time.sleep(1.5); Path(sys.argv[2]).touch()"
    )
    first = subprocess.Popen(
        [sys.executable, str(wrapper), "--", sys.executable, "-c", program, str(ready), str(done)],
        cwd=repository,
    )
    contender: subprocess.Popen[bytes] | None = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        first.kill()
        first.wait(timeout=5)
        contender = subprocess.Popen(
            [
                sys.executable,
                str(wrapper),
                "--",
                sys.executable,
                "-c",
                (
                    "import sys; from pathlib import Path; "
                    "assert Path(sys.argv[1]).exists(); Path(sys.argv[2]).touch()"
                ),
                str(done),
                str(second),
            ],
            cwd=linked,
        )
        assert contender.wait(timeout=8) == 0
        assert second.exists()
        # The persistent file is not a stale lock after process exit.
        result = subprocess.run(
            [sys.executable, str(wrapper), "--", sys.executable, "-c", "raise SystemExit(7)"],
            cwd=repository,
            timeout=5,
            check=False,
        )
        assert result.returncode == 7
    finally:
        if first.poll() is None:
            first.kill()
        first.wait(timeout=5)
        if contender is not None:
            if contender.poll() is None:
                contender.kill()
            contender.wait(timeout=5)
