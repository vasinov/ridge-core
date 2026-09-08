"""Standard-library deletion mechanism shared by local and remote resources."""

import os
import stat
from pathlib import Path
from typing import Literal


class DeletePathError(ValueError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def delete_path(root: Path, path: str, *, recursive: bool = False) -> Literal["deleted", "missing"]:
    requested = Path(path)
    if requested.is_absolute() or requested.name == "..":
        raise DeletePathError("invalid_path", "delete requires a relative non-root path")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise DeletePathError("path_type", "resource root is not a directory")
    # Resolve parents only: the final link is an entry to unlink, not a target to follow.
    unresolved = root / requested
    parent = unresolved.parent.resolve(strict=False)
    target = parent / unresolved.name
    if not parent.is_relative_to(root) or target == root:
        raise DeletePathError("invalid_path", "cannot delete the resource root or an escaping path")

    def remove(entry: Path) -> bool:
        try:
            mode = entry.lstat().st_mode
        except FileNotFoundError:
            return False
        if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            entry.unlink()
        elif stat.S_ISDIR(mode):
            if recursive:
                with os.scandir(entry) as children:
                    for child in children:
                        remove(Path(child.path))
            else:
                with os.scandir(entry) as children:
                    if next(children, None) is not None:
                        raise DeletePathError(
                            "path_type", "nonempty directory requires recursive=true"
                        )
            entry.rmdir()
        else:
            raise DeletePathError("path_type", "delete rejects special files")
        return True

    return "deleted" if remove(target) else "missing"
