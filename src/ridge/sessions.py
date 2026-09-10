"""Caller-owned lease renewal; neither a scheduler nor a remote execution fence."""

# Renewal and cleanup contain failures from trusted provider/client callbacks.
# ruff: noqa: BLE001

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, cast

from mcp import Client
from mcp.types import CallToolResult

from ridge.errors import LockOwnershipError
from ridge.model import JobScope, LockRequest

if TYPE_CHECKING:
    from ridge.application import RidgeService


def _report(errors: list[Exception], body: BaseException | None) -> None:
    if not errors:
        return
    if body is not None:
        for error in errors:
            body.add_note(str(error))
    elif len(errors) == 1:
        raise errors[0]
    else:
        raise ExceptionGroup("managed session failures", errors)


class ManagedSession:
    """Single-use synchronous workflow scope with a renewing, token-bound service."""

    def __init__(
        self,
        service: RidgeService,
        scopes: Sequence[JobScope],
        *,
        lease_seconds: float = 300,
        wait_seconds: float = 0,
    ) -> None:
        self._owner = service
        self._scopes = tuple(scopes)
        self._lease = lease_seconds
        self._wait = wait_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: LockOwnershipError | None = None
        self._deadline = 0.0
        self._entered = False
        self._open = False
        self._token = ""
        self._service: RidgeService | None = None

    @property
    def service(self) -> RidgeService:
        self.check()
        assert self._service is not None
        return self._service

    def check(self) -> None:
        """Raise when closed or unhealthy; does not interrupt already-admitted work."""
        if not self._open:
            raise LockOwnershipError("managed session is not open")
        if self._error is None and time.monotonic() >= self._deadline:
            self._error = LockOwnershipError("managed session renewal deadline elapsed")
        if self._error is not None:
            raise self._error

    def __enter__(self) -> Self:
        if self._entered:
            raise LockOwnershipError("managed sessions are single-use")
        self._entered = True
        value = self._owner.acquire_locks(
            self._scopes, lease_seconds=self._lease, wait_seconds=self._wait
        )
        self._token = str(value["token"])
        try:
            self._deadline = time.monotonic() + max(
                0, cast(float, value["expires_at"]) - time.time()
            )
            self._service = self._owner.with_lock(self._token)
            self._service._managed_check = self.check  # pyright: ignore[reportPrivateUsage]
            self._open = True
            self._thread = threading.Thread(target=self._renew, name="ridge-lease", daemon=True)
            self._thread.start()
        except BaseException as exc:
            self._open = False
            try:
                self._owner.release_locks(self._token)
            except Exception:
                exc.add_note("managed session release failed after heartbeat startup failure")
            raise
        return self

    def _renew(self) -> None:
        while not self._stop.wait(self._lease / 3):
            try:
                self.check()
                started = time.monotonic()
                self._owner.renew_locks(self._token)
                self.check()
                self._deadline = started + self._lease
            except Exception:
                self._error = LockOwnershipError(
                    "managed session renewal failed; further calls are disabled"
                )
                return

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        errors: list[Exception] = []
        try:
            self.check()
        except LockOwnershipError as error:
            errors.append(error)
        self._open = False
        try:
            self._owner.release_locks(self._token)
        except Exception:
            errors.append(LockOwnershipError("managed session release failed; inspect ownership"))
        _report(errors, exc)


class ManagedMCPSession:
    """Asyncio caller scope over an already-connected Ridge MCP client.

    The caller must keep the client and event loop alive throughout this scope.
    Tool results retain the MCP SDK's ordinary error/result representation.
    """

    _TOOLS = frozenset(
        {"execute", "list_data", "read_data", "write_data", "delete_data", "stat_data", "copy"}
    )

    def __init__(
        self,
        client: Client,
        scopes: Sequence[JobScope],
        *,
        lease_seconds: float = 300,
        wait_seconds: float = 0,
    ) -> None:
        self._client = client
        self._scopes = tuple(scopes)
        self._lease = lease_seconds
        self._wait = wait_seconds
        self._token = ""
        self._entered = False
        self._open = False
        self._deadline = 0.0
        self._error: LockOwnershipError | None = None
        self._task: asyncio.Task[None] | None = None

    async def _control(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        timeout = 70 if name == "acquire_locks" else 10
        if name == "renew_locks":
            timeout = min(timeout, self._lease / 3)
        async with asyncio.timeout(timeout):
            result = await self._client.call_tool(name, arguments)
        if result.is_error or result.structured_content is None:
            raise LockOwnershipError(f"managed session {name} failed")
        return result.structured_content

    def check(self) -> None:
        """Check health before dispatching the next workflow action."""
        if not self._open:
            raise LockOwnershipError("managed MCP session is not open")
        if self._error is None and time.monotonic() >= self._deadline:
            self._error = LockOwnershipError("managed MCP session renewal deadline elapsed")
        if self._error is not None:
            raise self._error

    async def __aenter__(self) -> Self:
        if self._entered:
            raise LockOwnershipError("managed sessions are single-use")
        self._entered = True
        value = await self._control(
            "acquire_locks",
            {
                "scopes": [
                    {
                        "resource": s.resource,
                        "operation": s.operation.value,
                        **(
                            {"path": s.path}
                            if isinstance(s, LockRequest) and s.path is not None
                            else {}
                        ),
                    }
                    for s in self._scopes
                ],
                "lease_seconds": self._lease,
                "wait_seconds": self._wait,
            },
        )
        self._token = str(value["token"])
        self._deadline = time.monotonic() + max(0, float(value["expires_at"]) - time.time())
        self._open = True
        self._task = asyncio.create_task(self._renew(), name="ridge-mcp-lease")
        return self

    async def _renew(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._lease / 3)
                self.check()
                started = time.monotonic()
                await self._control("renew_locks", {"token": self._token})
                self.check()
                self._deadline = started + self._lease
        except Exception:
            self._error = LockOwnershipError(
                "managed MCP session renewal failed; further calls are disabled"
            )

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> CallToolResult:
        """Dispatch a Ridge resource operation with this session's token."""
        self.check()
        if name not in self._TOOLS:
            raise ValueError(
                "use the original MCP client for observation and session control tools"
            )
        values = dict(arguments or {})
        if "lock_token" in values:
            raise ValueError("managed calls attach their own lock_token")
        values["lock_token"] = self._token
        return await self._client.call_tool(name, values)

    async def _close(self, body: BaseException | None) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        errors: list[Exception] = []
        try:
            self.check()
        except LockOwnershipError as error:
            errors.append(error)
        self._open = False
        try:
            await self._control("release_locks", {"token": self._token})
        except Exception:
            errors.append(
                LockOwnershipError("managed MCP session release failed; inspect ownership")
            )
        _report(errors, body)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        cleanup = asyncio.create_task(self._close(exc))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as cancelled:
            try:
                await cleanup
            except Exception:
                cancelled.add_note("managed MCP session cleanup failed; inspect ownership")
            raise
