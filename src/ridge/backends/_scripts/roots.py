"""Standalone filesystem view validation shared by host and remote helpers."""

from pathlib import Path, PurePosixPath


class RootViewError(ValueError):
    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(message)


def validate_root(root: str) -> None:
    if (
        not root
        or "\0" in root
        or PurePosixPath(root).is_absolute()
        or ".." in PurePosixPath(root).parts
    ):
        raise RootViewError("invalid_path", "data_root must be relative without '..' components")


def narrow_root(root: Path, roots: tuple[str, ...]) -> Path:
    for relative in roots:
        validate_root(relative)
        try:
            parent = root.resolve(strict=True)
            target = (parent / relative).resolve(strict=True)
            if not target.is_relative_to(parent):
                raise RootViewError("invalid_path", "data_root escapes its parent view")
            if not target.is_dir():
                raise RootViewError("path_type", "data_root is not a directory")
            root = target
        except FileNotFoundError as exc:
            raise RootViewError("path_not_found", "data_root does not exist") from exc
        except (OSError, RuntimeError) as exc:
            raise RootViewError("invalid_path", "cannot resolve data_root") from exc
    return root
