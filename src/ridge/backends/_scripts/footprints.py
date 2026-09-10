"""Read-only filesystem validation, called only under admitted path claims."""

import os
import stat
import sys
from pathlib import Path


def validate_filesystem_footprint(root: Path, relatives: tuple[str, ...]) -> bool:
    # Neither symlink traversal nor alternate mount coordinates are inferred.
    if sys.platform not in {"linux", "darwin"}:
        return False
    if root.anchor != "/" or ".." in root.parts:
        return False
    target = root.joinpath(*relatives)
    # Inspect lexical components, never resolve through an unprotected alias.
    metadata: os.stat_result | None = None
    for entry in (*reversed(target.parents), target):
        try:
            metadata = entry.lstat()
        except FileNotFoundError:
            if entry != target:
                return False  # Creating parents requires broader protection.
            metadata = None
            break
        if stat.S_ISLNK(metadata.st_mode):
            return False
        if entry.is_relative_to(root) and entry != root and entry.is_mount():
            return False
        if entry != target and not stat.S_ISDIR(metadata.st_mode):
            return False
    if sys.platform == "linux":
        try:
            # Use the kernel's spelling for mount comparisons, including roots
            # opened through case-insensitive or normalized directory names.
            descriptor = os.open(root, getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY)
            try:
                physical_root = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            finally:
                os.close(descriptor)
            for line in Path("/proc/self/mountinfo").read_text().splitlines():
                encoded = line.split()[4]
                for escape, literal in (
                    ("\\040", " "),
                    ("\\011", "\t"),
                    ("\\012", "\n"),
                    ("\\134", "\\"),
                ):
                    encoded = encoded.replace(escape, literal)
                mount = Path(encoded)
                if mount != physical_root and mount.is_relative_to(physical_root):
                    return False
        except (OSError, IndexError):
            return False
    if metadata is None:
        return True
    if stat.S_ISREG(metadata.st_mode):
        return metadata.st_nlink == 1
    if not stat.S_ISDIR(metadata.st_mode):
        return False
    # Bound work while holding claims. Larger or aliased trees retain broad locks.
    pending = [target]
    count = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for child in entries:
                count += 1
                if count > 4096:
                    return False
                info = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    return False
                if stat.S_ISDIR(info.st_mode):
                    path = Path(child.path)
                    if path.is_mount():
                        return False
                    pending.append(path)
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    return False
    return True
