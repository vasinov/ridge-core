"""Exercise metadata races and read budgets in real filesystem read processes."""

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ridge.backends._helper import HELPER_SOURCE

LOCAL_READ = """
from ridge.backends.local import LocalResource
from ridge.errors import OutputLimitExceededError
request = json.loads(sys.stdin.buffer.readline())
try:
    content = LocalResource("local", Path(sys.argv[1])).read(
        request["path"], max_bytes=request["max_bytes"]
    )
except OutputLimitExceededError as error:
    print(json.dumps({"ok": False, "error": "output_limit", "message": str(error)}))
else:
    print(json.dumps({"ok": True, "content": base64.b64encode(content).decode("ascii")}))
"""


@pytest.mark.parametrize("backend", ["local", "helper", "cli"])
@pytest.mark.parametrize("mutation", ["grow", "replace"])
@pytest.mark.parametrize("maximum", [0, 4])
def test_read_limits_survive_change_after_stat(
    tmp_path: Path, backend: str, mutation: str, maximum: int
) -> None:
    (tmp_path / "data.bin").write_bytes(b"")
    # Mutate only when the actual read opens the file, after all stat checks.
    injection = (
        f"MAXIMUM = {maximum!r}\nMUTATION = {mutation!r}\n"
        + r"""
import base64
import json
import sys
from pathlib import Path

original_open = Path.open
class BoundedStream:
    def __init__(self, stream):
        self.stream = stream
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.stream.close()
    def read(self, size=-1):
        assert size == MAXIMUM + 1, "read must request only the detection budget"
        result = self.stream.read(size)
        assert len(result) == MAXIMUM + 1
        return result

def opening(path, mode="r", *args, **kwargs):
    if path.name == "data.bin" and mode == "rb":
        replacement = path if MUTATION == "grow" else path.with_name("replacement.bin")
        with original_open(replacement, "wb") as output:
            output.write(b"x" * 131072)
        if MUTATION == "replace":
            replacement.replace(path)
        return BoundedStream(original_open(path, mode, *args, **kwargs))
    return original_open(path, mode, *args, **kwargs)
Path.open = opening
"""
    )
    source = HELPER_SOURCE if backend == "helper" else LOCAL_READ
    arguments = [str(tmp_path), "read"]
    if backend == "cli":
        config = tmp_path / "ridge.yaml"
        config.write_text("resources:\n  local:\n    provider: local\n    root: .\n")
        source = "\nfrom ridge.cli import main\nmain()\n"
        arguments = [
            "--config",
            str(config),
            "read",
            "local",
            "data.bin",
            "--max-bytes",
            str(maximum),
        ]
    completed = subprocess.run(
        [sys.executable, "-c", injection + source, *arguments],
        input=json.dumps({"path": "data.bin", "max_bytes": maximum}).encode() + b"\n",
        capture_output=True,
        timeout=5,
        check=False,
    )
    if backend == "cli":
        assert completed.returncode == 2
        assert b"byte limit" in completed.stderr
        assert not completed.stdout
        copied = subprocess.run(
            [
                sys.executable,
                "-c",
                "from ridge.cli import main; main()",
                "--config",
                str(tmp_path / "ridge.yaml"),
                "copy",
                "local:data.bin",
                "local:copied.bin",
            ],
            capture_output=True,
            timeout=10,
            check=True,
        )
        assert (tmp_path / "copied.bin").read_bytes() == b"x" * 131072
        assert not copied.stderr
        return
    assert completed.returncode == 0, completed.stderr
    response = json.loads(completed.stdout)
    assert response["ok"] is False
    assert response["error"] == "output_limit"
    assert "content" not in response
    assert (tmp_path / "data.bin").read_bytes() == b"x" * 131072


@pytest.mark.parametrize("backend", ["local", "helper"])
@pytest.mark.parametrize("maximum", [None, 0, 4])
@pytest.mark.parametrize("size", [0, 4, 5])
def test_read_boundaries(tmp_path: Path, backend: str, maximum: int | None, size: int) -> None:
    content = b"\xff" * size
    (tmp_path / "data.bin").write_bytes(content)
    source = HELPER_SOURCE if backend == "helper" else LOCAL_READ
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import base64,json,sys\nfrom pathlib import Path\n" + source,
            str(tmp_path),
            "read",
        ],
        input=json.dumps({"path": "data.bin", "max_bytes": maximum}).encode() + b"\n",
        capture_output=True,
        timeout=5,
        check=True,
    )
    response = json.loads(completed.stdout)
    if maximum is not None and size > maximum:
        assert response["error"] == "output_limit"
        assert "content" not in response
    else:
        assert base64.b64decode(response["content"]) == content
