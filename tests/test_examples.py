"""Keep the published CSV assets runnable with the documented local workflow."""

from __future__ import annotations

import ast
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

from ridge import AccessGrant, Operation, RidgeService


def test_readme_scope_example(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    blocks = re.findall(r"```python\n(.*?)```", (root / "README.md").read_text(), re.DOTALL)
    call = ast.parse(blocks[0]).body[0]
    assert isinstance(call, ast.Expr) and isinstance(call.value, ast.Call)
    grants: list[dict[str, Any]] = ast.literal_eval(call.value.keywords[0].value)
    policy = {grant["resource"]: grant["operations"] for grant in grants}
    for name in policy:
        (tmp_path / name).mkdir()
    (tmp_path / "results/comparison/a").mkdir(parents=True)
    config = tmp_path / "ridge.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "resources": {name: {"provider": "local", "root": name} for name in policy},
                "permissions": policy,
                "delegation": policy,
            }
        )
    )
    parent = RidgeService.from_config(config)
    issued = parent.create_scope(
        [
            AccessGrant(
                grant["resource"],
                frozenset(Operation(op) for op in grant["operations"]),
                data_root=grant.get("data_root"),
            )
            for grant in grants
        ]
    )
    child = RidgeService.from_config(config, scope_token=issued.token)
    child.write_data("results", "metrics.json", b'{"score": 10}')
    assert (tmp_path / "results/comparison/a/metrics.json").read_bytes() == b'{"score": 10}'
    parent.revoke_scope(issued.scope.id)


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
