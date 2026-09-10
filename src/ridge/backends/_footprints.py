"""Conservative lexical plans paired with protected filesystem validation."""

from collections.abc import Callable
from pathlib import PurePosixPath

from ridge.claims import Footprint
from ridge.model import Operation


class FilesystemFootprints:
    def __init__(
        self, coordinate: tuple[str, ...], validate: Callable[[tuple[str, ...]], bool]
    ) -> None:
        self._coordinate = coordinate
        self._validate = validate

    def coordinate_space(self) -> tuple[str, ...]:
        return self._coordinate

    def plan_footprint(
        self, operation: Operation, path: str, roots: tuple[str, ...]
    ) -> tuple[Footprint, ...] | None:
        if operation == Operation.COMPUTE_EXEC:
            return None
        parts: list[str] = []
        for text in (*roots, path):
            value = PurePosixPath(text)
            if not text or "\0" in text or value.is_absolute() or ".." in value.parts:
                return None
            # Staging is observable through Ridge, but never a narrow user target.
            if any(
                part.casefold().startswith(".ridge-")
                or part.endswith(".")
                or any(
                    character
                    not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                    for character in part
                )
                for part in value.parts
            ):
                return None
            parts.extend(part.casefold() for part in value.parts)
        return (
            Footprint(
                ("filesystem", *parts), "shared" if operation.effect == "read" else "exclusive"
            ),
        )

    def validate_footprint(self, path: str, roots: tuple[str, ...]) -> bool:
        return self._validate((*roots, path))
