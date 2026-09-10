"""Pure capability planning using the full inventory's canonical coordinate groups."""

# Validate return values from trusted but potentially incorrectly implemented providers.
# pyright: reportUnnecessaryIsInstance=false

import json
from collections.abc import Mapping, Sequence
from typing import cast

from ridge.claims import Claim, Footprint, covers, normalize
from ridge.model import JobScope
from ridge.registry import ResourceRegistry


class FootprintPlanner:
    def __init__(
        self,
        registry: ResourceRegistry,
        keys: Mapping[str, str],
        roots: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.registry = registry
        self.keys = keys
        self.roots = roots or {}

    def plan(
        self, scopes: Sequence[JobScope], paths: Sequence[str | None] | None = None
    ) -> tuple[Claim, ...]:
        if paths is None:
            paths = (None,) * len(scopes)
        if len(paths) != len(scopes):
            raise ValueError("each operation scope requires one target")
        claims: list[Claim] = []
        for action, path in zip(scopes, paths, strict=True):
            domain = self.keys.get(action.resource, action.resource)
            mode = "shared" if action.operation.effect == "read" else "exclusive"
            fallback = Claim(None, mode, domain)
            provider = self.registry.get(action.resource).capabilities.footprints
            if provider is None or path is None:
                claims.append(fallback)
                continue
            coordinate = provider.coordinate_space()
            self._validate_coordinate(coordinate)
            compatible = coordinate is not None
            for name in self.registry.names():
                if self.keys.get(name, name) != domain:
                    continue
                alias = self.registry.get(name).capabilities.footprints
                alias_coordinate = alias.coordinate_space() if alias else None
                self._validate_coordinate(alias_coordinate)
                if alias is None or alias_coordinate != coordinate:
                    compatible = False
                    break
            if not compatible:
                claims.append(fallback)
                continue
            planned = provider.plan_footprint(
                action.operation, path, self.roots.get(action.resource, ())
            )
            if planned is None:
                claims.append(fallback)
                continue
            if (
                not isinstance(planned, tuple)
                or not planned
                or any(not isinstance(item, Footprint) for item in planned)
            ):
                raise ValueError("provider returned an invalid footprint plan")
            # A mutating operation may never downgrade its effects to shared.
            if mode == "exclusive" and any(item.mode != mode for item in planned):
                raise ValueError("mutating footprint requires exclusive claims")
            if len(planned) > 64 or any(
                item.scope is not None
                and (len(item.scope) > 32 or len(json.dumps(item.scope).encode()) > 16384)
                for item in planned
            ):
                claims.append(
                    Claim(
                        None,
                        "exclusive" if any(item.mode == "exclusive" for item in planned) else mode,
                        domain,
                    )
                )
            else:
                claims.extend(Claim(item.scope, item.mode, domain) for item in planned)
        return normalize(claims)

    def validate(
        self,
        scopes: Sequence[JobScope],
        paths: Sequence[str | None] | None,
        claims: Sequence[Claim],
    ) -> bool:
        """No dispatch: the caller must already own every candidate claim."""
        for action, path in zip(scopes, paths or (None,) * len(scopes), strict=True):
            domain = self.keys.get(action.resource, action.resource)
            mode = "shared" if action.operation.effect == "read" else "exclusive"
            if path is None or covers(claims, (Claim(None, mode, domain),)):
                continue
            guard = self.registry.get(action.resource).capabilities.footprint_guard
            if guard is not None:
                valid = guard.validate_footprint(path, self.roots.get(action.resource, ()))
                if type(valid) is not bool:
                    raise ValueError("footprint guard must return a boolean")
                if not valid:
                    return False
        return True

    @staticmethod
    def _validate_coordinate(coordinate: object) -> None:
        if coordinate is not None and (
            not isinstance(coordinate, tuple)
            or not coordinate
            or any(
                not isinstance(part, str) or not part
                for part in cast(tuple[object, ...], coordinate)
            )
        ):
            raise ValueError("provider returned an invalid coordinate space")
