from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from ridge.errors import AuthorizationDeniedError
from ridge.model import Operation


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """One resource operation and the request context used to authorize it."""

    resource: str
    operation: Operation
    context: Mapping[str, object]

    @classmethod
    def create(
        cls,
        resource: str,
        operation: Operation,
        context: Mapping[str, object] | None = None,
    ) -> AuthorizationRequest:
        return cls(resource, operation, MappingProxyType(dict(context or {})))


class Authorizer(Protocol):
    """Application-boundary authorization contract."""

    def allows(self, resource: str, operation: Operation) -> bool: ...

    def authorize(self, request: AuthorizationRequest) -> None: ...


class AuthorizationPolicy:
    """An unrestricted policy or exact, default-deny resource grants."""

    def __init__(
        self,
        grants: Mapping[str, frozenset[Operation]] | None = None,
    ) -> None:
        self._unrestricted = grants is None
        self._grants = MappingProxyType(dict(grants or {}))

    @classmethod
    def unrestricted(cls) -> AuthorizationPolicy:
        return cls()

    @classmethod
    def exact(cls, grants: Mapping[str, frozenset[Operation]]) -> AuthorizationPolicy:
        return cls(grants)

    @property
    def unrestricted_mode(self) -> bool:
        return self._unrestricted

    def allows(self, resource: str, operation: Operation) -> bool:
        return self._unrestricted or operation in self._grants.get(resource, frozenset())

    def authorize(self, request: AuthorizationRequest) -> None:
        if not self.allows(request.resource, request.operation):
            raise AuthorizationDeniedError(
                f"authorization denied for {request.operation.value} on resource "
                f"{request.resource!r}"
            )
