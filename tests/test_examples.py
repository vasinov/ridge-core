"""Keep the published CSV assets runnable with the documented local workflow."""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import yaml

from ridge import RidgeService


def test_readme_inventory_matches_runnable_example() -> None:
    root = Path(__file__).resolve().parents[1]
    inventories = re.findall(r"```yaml\n(.*?)```", (root / "README.md").read_text(), re.DOTALL)
    assert len(inventories) == 1
    expected = yaml.safe_load((root / "docs/examples/assets/ridge.yaml").read_text())
    assert yaml.safe_load(inventories[0]) == expected


def test_csv_walkthrough_assets(tmp_path: Path) -> None:
    assets = Path(__file__).resolve().parents[1] / "docs" / "examples" / "assets"
    for name in ("inputs", "worker", "reports"):
        (tmp_path / name).mkdir()
    shutil.copyfile(assets / "ridge.yaml", tmp_path / "ridge.yaml")
    for name in ("sales.csv", "analyze.py"):
        shutil.copyfile(assets / name, tmp_path / "inputs" / name)

    service = RidgeService.from_config(tmp_path / "ridge.yaml")
    for name in ("sales.csv", "analyze.py"):
        service.copy(f"inputs:{name}", f"worker:{name}")
    result = service.execute("worker", [sys.executable, "analyze.py"], timeout_seconds=10)
    assert result.exit_code == 0
    assert result.stderr == b""
    expected = b"region,revenue\nEast,100.00\nWest,200.00\n"
    assert result.stdout == expected
    service.copy("worker:report.csv", "reports:report.csv")
    assert service.read_data("reports", "report.csv") == expected
    for name in ("sales.csv", "analyze.py"):
        assert (tmp_path / "inputs" / name).read_bytes() == (assets / name).read_bytes()
        assert (tmp_path / "worker" / name).read_bytes() == (assets / name).read_bytes()
