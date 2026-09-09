"""Exercise the published host handoff against real local child/job processes."""

import json
import subprocess
import sys
from pathlib import Path


def test_delegation_example(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "docs/examples/assets/delegate.py"
    workspace = tmp_path / "workspace"
    result = subprocess.run(
        [sys.executable, str(script), str(workspace)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["scores"] == {"a": {"score": 10}, "b": {"score": 4}}
    assert report["child_scopes_closed"]
    assert len(set(report["jobs"])) == 2
    assert (workspace / "results/a/metrics.json").read_text() == '{"score": 10}'
    assert (workspace / "results/b/metrics.json").read_text() == '{"score": 4}'
    assert not (workspace / "inputs/forbidden.txt").exists()
    rerun = subprocess.run(
        [sys.executable, str(script), str(workspace)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert rerun.returncode != 0
    assert (workspace / "results/a/metrics.json").read_text() == '{"score": 10}'
