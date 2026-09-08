"""Run a command under the repository-wide POSIX integration lock.

Usage: python scripts/with_integration_lock.py -- COMMAND [ARG ...]
Run from the designated integration checkout. Exit 75 means lock contention.
"""

from __future__ import annotations

import fcntl
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    command = sys.argv[1:]
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        print(__doc__, file=sys.stderr)
        return 2
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with (Path(common) / "ridge-integration.lock").open("a+b") as lock:
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print(
                        "Integration is busy; retry after the current integrator finishes.",
                        file=sys.stderr,
                    )
                    return 75
                time.sleep(0.1)
        # The child retains ownership if this wrapper dies before its command ends.
        result = subprocess.run(command, pass_fds=(lock.fileno(),), check=False)
        return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
