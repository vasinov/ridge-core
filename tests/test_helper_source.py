"""Packaged helper source remains standalone and safe to inspect without dispatch."""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ridge.backends._source import HELPER_SOURCE, TRANSFER_HELPER_SOURCE


@pytest.mark.parametrize("source", [HELPER_SOURCE, TRANSFER_HELPER_SOURCE])
def test_helper_source_is_standalone_importable_python(source: str, tmp_path: Path) -> None:
    tree = ast.parse(source)
    # Type-checking-only imports describe prepended definitions, never remote dependencies.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            node.body = []
    # Helpers are shipped to Python without Ridge or third-party dependencies.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
            assert node.level == 0
        else:
            continue
        assert all(module.split(".")[0] in sys.stdlib_module_names for module in modules)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; namespace = {'__name__': 'inspection'}; exec(compile(sys.stdin.read(), '<helper>', 'exec'), namespace); assert callable(namespace['main'])",
        ],
        input=source,
        text=True,
        capture_output=True,
        timeout=5,
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert not list(tmp_path.iterdir())


def test_operations_helper_runs_without_ridge_installed(tmp_path: Path) -> None:
    (tmp_path / "data").write_bytes(b"hello")
    result = subprocess.run(
        [sys.executable, "-I", "-c", HELPER_SOURCE, str(tmp_path), "read"],
        input=b'{"path":"data","max_bytes":5}\n',
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"ok": True, "content": "aGVsbG8="}
    assert result.stderr == b""
