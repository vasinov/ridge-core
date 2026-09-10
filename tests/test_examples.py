"""Keep the published delegation snippet and CSV workflow runnable."""

from __future__ import annotations

import ast
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from ridge import AccessGrant, AuthorizationDeniedError, Operation, RidgeService


def test_delegation_guide_read_only_scope(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    guide = (root / "docs/guides/delegation.md").read_text()
    section = guide.split("## Create task access\n", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"```python\n(.*?)```", section, re.DOTALL)
    assert len(blocks) == 1, "Expected one Python scope-creation example in the delegation guide"
    call = ast.parse(blocks[0]).body[0]
    assert isinstance(call, ast.Expr) and isinstance(call.value, ast.Call)
    assert isinstance(call.value.func, ast.Name) and call.value.func.id == "create_scope"
    grants: list[dict[str, Any]] = ast.literal_eval(
        next(keyword.value for keyword in call.value.keywords if keyword.arg == "grants")
    )
    policy = {"inputs": ["data.read", "data.stat", "data.write"]}
    (tmp_path / "inputs").mkdir()
    source = tmp_path / "inputs/sample.txt"
    source.write_bytes(b"original")
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
                frozenset(Operation(op) for op in grant.get("delegation", [])),
                data_root=grant.get("data_root"),
            )
            for grant in grants
        ]
    )
    child = RidgeService.from_config(config, scope_token=issued.token)
    assert child.read_data("inputs", "sample.txt") == b"original"
    assert child.stat_data("inputs", "sample.txt").size == len(b"original")
    with pytest.raises(AuthorizationDeniedError):
        child.write_data("inputs", "sample.txt", b"changed")
    with pytest.raises(AuthorizationDeniedError):
        child.create_scope([AccessGrant("inputs", frozenset({Operation.DATA_READ}))])
    assert source.read_bytes() == b"original"
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
