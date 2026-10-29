"""In-memory ToolGateway fake: canned payloads per server.

Each server maps to a payload returned by ``call`` (any shape the
retrieval normalisation understands) or to an ``Exception`` instance
that is raised instead. Unknown servers raise :class:`ToolCallError`,
the same way the real gateway does when a session is missing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from iso_insight.exceptions import ToolCallError
from iso_insight.ports.tools import ToolGateway
from iso_insight.schemas.mcp import ToolStatus


class FakeToolGateway(ToolGateway):
    def __init__(self, sessions: dict[str, Any] | None = None) -> None:
        self.sessions: dict[str, Any] = dict(sessions or {})
        self.startup_errors: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self.started = False
        self.stopped = False
        # Every refresh() tick, with the timeout it was given (None = default).
        self.refresh_calls: list[float | None] = []
        # Test hook: simulate a server coming up during a refresh tick.
        self.on_refresh: Callable[[], None] | None = None

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def refresh(self, timeout: float | None = None) -> ToolStatus:
        self.refresh_calls.append(timeout)
        if self.on_refresh is not None:
            self.on_refresh()
        return self.status()

    async def call(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        self.calls.append(
            {"server": server, "tool": tool, "arguments": dict(arguments), "timeout": timeout}
        )
        if server not in self.sessions:
            raise ToolCallError(f"brak sesji dla serwera: {server}")
        payload = self.sessions[server]
        if isinstance(payload, Exception):
            raise payload
        return payload

    def status(self) -> ToolStatus:
        return ToolStatus(
            connected=sorted(self.sessions),
            startup_errors=list(self.startup_errors),
        )
