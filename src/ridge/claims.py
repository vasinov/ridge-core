"""Typed, backend-neutral interference footprints and their overlap algebra."""

# Runtime validation protects the provider boundary as well as typed callers.
# pyright: reportUnnecessaryIsInstance=false

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Literal

LockMode = Literal["shared", "exclusive"]
LockScope = tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class Footprint:
    scope: LockScope
    mode: LockMode

    def __post_init__(self) -> None:
        if self.mode not in ("shared", "exclusive"):
            raise ValueError("invalid footprint mode")
        if self.scope is not None and (
            not isinstance(self.scope, tuple)
            or not self.scope
            or any(not isinstance(part, str) or not part for part in self.scope)
        ):
            raise ValueError("scope must be None or a nonempty tuple of nonempty strings")


@dataclass(frozen=True, slots=True)
class Claim(Footprint):
    domain: str


def contains(parent: LockScope, child: LockScope) -> bool:
    return parent is None or (child is not None and child[: len(parent)] == parent)


def covers(held: Sequence[Claim], needed: Sequence[Claim]) -> bool:
    return all(
        any(
            left.domain == right.domain
            and contains(left.scope, right.scope)
            and (left.mode == "exclusive" or right.mode == "shared")
            for left in held
        )
        for right in needed
    )


def conflicts(left: Sequence[Claim], right: Sequence[Claim]) -> bool:
    return any(
        a.domain == b.domain
        and "exclusive" in (a.mode, b.mode)
        and (contains(a.scope, b.scope) or contains(b.scope, a.scope))
        for a in left
        for b in right
    )


def normalize(claims: Sequence[Claim]) -> tuple[Claim, ...]:
    unique = set(claims)
    return tuple(
        sorted(
            (claim for claim in unique if not covers(tuple(unique - {claim}), (claim,))),
            key=lambda c: (c.domain, c.scope is not None, c.scope or (), c.mode),
        )
    )


def encode_claims(claims: Sequence[Claim]) -> str:
    return json.dumps([asdict(claim) for claim in claims])


def decode_claims(value: str) -> tuple[Claim, ...]:
    return tuple(
        Claim(None if row["scope"] is None else tuple(row["scope"]), row["mode"], row["domain"])
        for row in json.loads(value)
    )
