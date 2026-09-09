"""Shared scope transport values and startup-only bearer binding."""

from __future__ import annotations

import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ridge._access import AccessGrant, ScopeAccessError, ScopeInfo, ScopeStatus
from ridge.model import Operation


def read_scope_token(path: Path | None) -> str | None:
    """Explicit token file wins over the environment; empty presence fails closed."""
    if path is None:
        if "RIDGE_SCOPE_TOKEN" not in os.environ:
            return None
        token = os.environ["RIDGE_SCOPE_TOKEN"]
    else:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ScopeAccessError("invalid_token_file")
                content = handle.read(260)
            if len(content) > 258:
                raise ScopeAccessError("invalid_token_file")
            token = content.decode("ascii").rstrip("\r\n")
        except (OSError, UnicodeError) as exc:
            raise ScopeAccessError("invalid_token_file") from exc
    if not token or len(token) > 256 or not token.isascii():
        raise ScopeAccessError("invalid_token")
    return token


class GrantModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    resource: str
    operations: list[Operation] = Field(default_factory=lambda: list[Operation]())
    delegation: list[Operation] = Field(default_factory=lambda: list[Operation]())
    data_root: str | None = Field(
        default=None,
        description=(
            "Optional relative data view for this grant's resource only. For example, "
            "resource='results', data_root='task' restricts results data to task/. "
            "Omit to inherit the parent's view. Does not change compute.exec's working "
            "directory or restrict execution. Filesystem view directories must already exist."
        ),
    )

    @field_validator("operations", "delegation")
    @classmethod
    def unique(cls, values: list[Operation]) -> list[Operation]:
        if len(set(values)) != len(values):
            raise ValueError("operation lists must not contain duplicates")
        return values

    def grant(self) -> AccessGrant:
        return AccessGrant(
            self.resource, frozenset(self.operations), frozenset(self.delegation), self.data_root
        )


class ScopeModel(BaseModel):
    id: str
    parent_id: str | None
    grants: list[GrantModel]
    created_at: datetime
    expires_at: datetime | None
    status: ScopeStatus

    @classmethod
    def from_scope(cls, scope: ScopeInfo) -> ScopeModel:
        return cls(
            id=scope.id,
            parent_id=scope.parent_id,
            grants=[
                GrantModel(
                    resource=g.resource,
                    operations=sorted(g.operations),
                    delegation=sorted(g.delegation),
                    data_root=g.data_root,
                )
                for g in scope.grants
            ],
            created_at=scope.created_at,
            expires_at=scope.expires_at,
            status=scope.status,
        )


class IssuedScopeModel(BaseModel):
    scope: ScopeModel
    token: str = Field(repr=False)


class ScopePageModel(BaseModel):
    scopes: list[ScopeModel]
    next_cursor: str | None


class EffectiveGrantModel(GrantModel):
    data_root_chain: list[str]


class AccessModel(BaseModel):
    scope_id: str | None
    mode: Literal["operator", "scope"]
    resources: list[EffectiveGrantModel]
