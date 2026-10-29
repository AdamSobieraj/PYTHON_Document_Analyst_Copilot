"""MCP tool gateway: one long-lived client session per configured server.

Implements the :class:`~iso_insight.ports.tools.ToolGateway` port over
the ``mcp`` SDK. Startup is best-effort per server: a failing server is
recorded in ``ToolStatus.startup_errors`` and the workflow degrades to
the sources that did load. ``refresh`` re-attempts only the servers that
are not connected, so a worker picks up upstream services that start
later without a restart. Tool-call failures are mapped onto the domain
exceptions (``TransientToolCallError`` for timeouts and transport
errors, ``ToolCallError`` for permanent ones).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any

from iso_insight.config import Settings
from iso_insight.exceptions import ToolCallError, TransientToolCallError
from iso_insight.ports.tools import ToolGateway
from iso_insight.schemas.mcp import MCPServerConfig, ToolStatus

logger = logging.getLogger("iso_insight.tools")

try:  # the SDK is optional at import time — the gateway degrades gracefully without it
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client
except Exception:  # pragma: no cover - exercised only when mcp is absent
    ClientSession = None  # type: ignore[assignment,misc]
    sse_client = None  # type: ignore[assignment]
    streamable_http_client = None  # type: ignore[assignment]

try:  # mcp 2.x wants an owned httpx2 client; 1.x takes headers/timeout directly
    import httpx2
except Exception:  # pragma: no cover - version-dependent
    httpx2 = None  # type: ignore[assignment]


def _streamable_http_params() -> set[str]:
    """Parameter names of the installed streamable_http_client (mcp version detection)."""
    if streamable_http_client is None:
        return set()
    try:
        return set(inspect.signature(streamable_http_client).parameters)
    except (TypeError, ValueError):
        return set()


def _result_field(result: Any, *names: str) -> Any:
    """First non-None attribute among ``names`` (SDK field spelling varies by version)."""
    for name in names:
        value = getattr(result, name, None)
        if value is not None:
            return value
    return None


def _parse_mcp_result(result: Any) -> Any:
    """Extract the tool payload: structured content first, else concatenated text blocks."""
    structured = _result_field(result, "structured_content", "structuredContent")
    if structured is not None:
        return structured
    blocks = _result_field(result, "content") or []
    parts = [block.get("text", "") for block in blocks if isinstance(block, dict)]
    combined = "\n".join(part for part in parts if part).strip()
    if not combined:
        return {}
    try:
        return json.loads(combined)
    except json.JSONDecodeError:
        return {"text": combined}


class MCPToolGateway(ToolGateway):
    """One MCP session per configured server; start/stop are idempotent."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sessions: dict[str, Any] = {}
        self._contexts: list[tuple[str, Any, Any]] = []
        self._owned_clients: dict[str, Any] = {}
        # Last recorded failure reason per configured server (dropped on success).
        self._last_errors: dict[str, str] = {}
        # Servers with a connection attempt in flight (guards monitor ticks
        # against gate probes racing on the same server).
        self._pending: set[str] = set()

    async def start(self) -> None:
        """Open a session to every configured server; failures are collected, not raised."""
        self._last_errors = {}
        if self._contexts:
            await self.stop()
        if ClientSession is None or streamable_http_client is None or sse_client is None:
            for server_name in self._settings.mcp_servers:
                self._last_errors[server_name] = "brak biblioteki mcp"
            return
        await self._connect_missing(timeout=None)

    async def refresh(self, timeout: float | None = None) -> ToolStatus:
        """Best-effort reconnect of the servers that are not connected (see the port)."""
        if (
            ClientSession is not None
            and streamable_http_client is not None
            and sse_client is not None
        ):
            await self._connect_missing(timeout)
        return self.status()

    async def _connect_missing(self, timeout: float | None) -> None:
        """Connect every configured server that is not (yet) connected.

        Attempts run in parallel (gathered), so a monitor tick or a gate
        probe never serialises on the slowest dead server.
        """
        attempts = [
            self._attempt_one(server_name, cfg, timeout)
            for server_name, cfg in self._settings.mcp_servers.items()
            if server_name not in self._sessions and server_name not in self._pending
        ]
        if attempts:
            await asyncio.gather(*attempts)

    async def _attempt_one(
        self, server_name: str, cfg: MCPServerConfig, timeout: float | None
    ) -> None:
        """Connect one server in a child task; record the outcome in ``_last_errors``.

        One child task per server: the MCP SDK's internal anyio cancel scopes
        cancel the task they run in. A dead server must die in its own task,
        otherwise the leaked CancelledError poisons every later anyio
        checkpoint in this process (the task's cancelling() stays > 0).
        """
        limit = cfg.timeout if timeout is None else timeout
        self._pending.add(server_name)
        try:
            task = asyncio.create_task(
                self._start_one(server_name, cfg, limit), name=f"mcp-start-{server_name}"
            )
            try:
                await asyncio.wait_for(task, timeout=limit)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling() > 0:
                    # Cancelled from outside (shutdown, test teardown) — propagate.
                    task.cancel()
                    raise
                self._last_errors[server_name] = "przerwane podczas startu"
            except TimeoutError:
                self._last_errors[server_name] = f"timeout startu ({limit:.0f}s)"
            except BaseException as exc:
                logger.exception("MCP server connection failed: %s", server_name)
                self._last_errors[server_name] = exc.__class__.__name__
            else:
                self._last_errors.pop(server_name, None)
        finally:
            self._pending.discard(server_name)

    async def _start_one(
        self, server_name: str, cfg: MCPServerConfig, timeout: float | None = None
    ) -> None:
        limit = cfg.timeout if timeout is None else timeout
        if cfg.transport == "streamable_http":
            if "http_client" in _streamable_http_params() and httpx2 is not None:
                owned_client = httpx2.AsyncClient(
                    headers=cfg.headers, timeout=httpx2.Timeout(limit)
                )
                self._owned_clients[server_name] = owned_client
                client_cm = streamable_http_client(cfg.url, http_client=owned_client)
            else:  # mcp 1.x signature
                client_cm = streamable_http_client(  # type: ignore[call-arg]
                    url=cfg.url, headers=cfg.headers, timeout=limit
                )
        elif cfg.transport == "sse":
            client_cm = sse_client(url=cfg.url, headers=cfg.headers, timeout=limit)
        else:
            raise ToolCallError(f"Nieobsługiwany transport: {cfg.transport}")
        session: Any = None
        session_entered = False
        try:
            entered = await client_cm.__aenter__()
            read, write = entered
            session = ClientSession(read, write)
            await session.__aenter__()
            session_entered = True
            await session.initialize()
        except BaseException:
            # initialize() can fail after the session was entered — close it then,
            # not just the client context.
            await self._teardown_one(server_name, client_cm, session if session_entered else None)
            self._owned_clients.pop(server_name, None)
            raise
        self._sessions[server_name] = session
        self._contexts.append((server_name, client_cm, session))

    async def _teardown_one(self, server_name: str, client_cm: Any, session: Any | None) -> None:
        if session is not None:
            try:
                await session.__aexit__(None, None, None)
            except Exception:
                logger.debug("MCP session close failed: %s", server_name, exc_info=True)
        try:
            await client_cm.__aexit__(None, None, None)
        except Exception:
            logger.debug("MCP client close failed: %s", server_name, exc_info=True)
        owned_client = self._owned_clients.pop(server_name, None)
        if owned_client is not None:
            try:
                await owned_client.aclose()
            except Exception:
                logger.debug("MCP http client close failed: %s", server_name, exc_info=True)

    async def stop(self) -> None:
        """Close every session (idempotent — safe to call twice)."""
        while self._contexts:
            server_name, client_cm, session = self._contexts.pop()
            await self._teardown_one(server_name, client_cm, session)
            self._sessions.pop(server_name, None)
        self._owned_clients.clear()

    async def call(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        """Call one tool on one server and return its parsed payload."""
        session = self._sessions.get(server)
        if session is None:
            raise ToolCallError(f"Serwer MCP niedostępny: {server}")
        try:
            result = await asyncio.wait_for(session.call_tool(tool, arguments), timeout=timeout)
        except TimeoutError as exc:
            raise TransientToolCallError(f"Timeout wywołania narzędzia: {server}/{tool}") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The workflow degrades to "no sources" on any tool failure — a single
            # transient mapping keeps callers simple (CLAUDE.md: retry keys off TransientError).
            raise TransientToolCallError(
                f"Wywołanie narzędzia niepowodzone: {server}/{tool}: {exc}"
            ) from exc
        if _result_field(result, "isError", "is_error"):
            raise ToolCallError(f"Narzędzie zwróciło błąd: {server}/{tool}")
        return _parse_mcp_result(result)

    def status(self) -> ToolStatus:
        """Connected servers plus the last recorded error for each missing one."""
        return ToolStatus(
            connected=sorted(self._sessions),
            startup_errors=[
                f"{name}: {self._last_errors[name]}"
                for name in self._settings.mcp_servers
                if name not in self._sessions and name in self._last_errors
            ],
        )
