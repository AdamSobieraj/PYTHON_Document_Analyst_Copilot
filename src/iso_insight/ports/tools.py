"""Port: tool gateway — the only path from the domain to external MCP servers.

RAG knowledge bases, Jira and Confluence are all "tools" behind this
interface; which servers exist is pure configuration
(``Settings.mcp_servers``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from iso_insight.schemas.mcp import ToolStatus


class ToolGateway(ABC):
    """Manages per-server MCP sessions and executes tool calls.

    A failure of one server never takes down the others: startup errors
    are collected (see :meth:`status`) and the workflow degrades
    gracefully.
    """

    @abstractmethod
    async def start(self) -> None:
        """Open sessions for all configured servers (failures collected, not raised)."""

    @abstractmethod
    async def stop(self) -> None:
        """Tear down all sessions."""

    @abstractmethod
    async def call(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        """Call one tool on one server; returns the parsed result payload.

        Raises :class:`~iso_insight.exceptions.ToolCallError`, or
        :class:`~iso_insight.exceptions.TransientToolCallError` when the
        failure is retryable (timeout, 429, transport blip).
        """

    @abstractmethod
    def status(self) -> ToolStatus:
        """Connected server names plus the collected startup errors."""
