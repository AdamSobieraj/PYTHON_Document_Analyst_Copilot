"""MCP tool gateway: one long-lived client session per configured endpoint.

Implements the :class:`~iso_insight.ports.tools.ToolGateway` port over
the ``mcp`` SDK. Several configured servers may point at the same
endpoint (transport + url + headers, e.g. the three RAG names on one
native server) — they share one physical session, while the status stays
per server name. Startup is best-effort per endpoint: a failing endpoint
is recorded in ``ToolStatus.startup_errors`` (for every name on it) and
the workflow degrades to the sources that did load. ``refresh`` re-attempts
only the servers that are not connected, so a worker picks up upstream
services that start later without a restart. Tool-call failures are mapped
onto the domain exceptions (``TransientToolCallError`` for timeouts and
transport errors, ``ToolCallError`` for permanent ones — with the
server's error text, so schema rejections are visible in the audit trail).
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

# (transport, url, sorted headers) — the identity of one physical MCP endpoint.
EndpointKey = tuple[str, str, tuple[tuple[str, str], ...]]

# How much of the server's error text lands in the domain exception.
_ERROR_DETAIL_LIMIT = 300


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


def _result_text(result: Any) -> str:
    """Concatenated text blocks of the tool result (empty when there are none)."""
    blocks = _result_field(result, "content") or []
    parts = [block.get("text", "") for block in blocks if isinstance(block, dict)]
    return "\n".join(part for part in parts if part).strip()


def _error_detail(result: Any) -> str:
    """The server's error text for the exception message, truncated with an ellipsis."""
    text = _result_text(result)
    if not text:
        return ""
    if len(text) > _ERROR_DETAIL_LIMIT:
        text = text[:_ERROR_DETAIL_LIMIT] + "…"
    return f": {text}"


def _parse_mcp_result(result: Any) -> Any:
    """Extract the tool payload: structured content first, else concatenated text blocks."""
    structured = _result_field(result, "structured_content", "structuredContent")
    if structured is not None:
        return structured
    combined = _result_text(result)
    if not combined:
        return {}
    try:
        return json.loads(combined)
    except json.JSONDecodeError:
        return {"text": combined}


class MCPToolGateway(ToolGateway):
    """One MCP session per endpoint; names on it share the session. Idempotent."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sessions: dict[str, Any] = {}
        self._endpoints: dict[EndpointKey, tuple[Any, Any]] = {}
        self._owned_clients: dict[EndpointKey, Any] = {}
        # Last recorded failure reason per configured server (dropped on success).
        self._last_errors: dict[str, str] = {}
        # Servers with a connection attempt in flight (guards monitor ticks
        # against gate probes racing on the same server).
        self._pending: set[str] = set()

    @staticmethod
    def _endpoint_key(cfg: MCPServerConfig) -> EndpointKey:
        return (cfg.transport, cfg.url, tuple(sorted(cfg.headers.items())))

    async def start(self) -> None:
        """Open a session to every configured endpoint; failures are collected, not raised."""
        self._last_errors = {}
        if self._endpoints:
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

        Servers sharing one endpoint share one physical session; names on
        an already-connected endpoint are attached without a new
        connection. Attempts run in parallel (gathered), so a monitor tick
        or a gate probe never serialises on the slowest dead server.
        """
        groups: dict[EndpointKey, list[str]] = {}
        for server_name, cfg in self._settings.mcp_servers.items():
            if server_name in self._sessions or server_name in self._pending:
                continue
            endpoint = self._endpoint_key(cfg)
            if endpoint in self._endpoints:
                # Already connected (another name on the same endpoint).
                _, session = self._endpoints[endpoint]
                self._sessions[server_name] = session
                continue
            groups.setdefault(endpoint, []).append(server_name)
        if groups:
            attempts = [
                self._attempt_endpoint(endpoint, names, timeout)
                for endpoint, names in groups.items()
            ]
            await asyncio.gather(*attempts)

    async def _attempt_endpoint(
        self,
        endpoint: EndpointKey,
        names: list[str],
        timeout: float | None,
    ) -> None:
        """Connect one endpoint in a child task; record the outcome for every name on it.

        One child task per endpoint: the MCP SDK's internal anyio cancel scopes
        cancel the task they run in. A dead server must die in its own task,
        otherwise the leaked CancelledError poisons every later anyio
        checkpoint in this process (the task's cancelling() stays > 0).
        """
        cfgs = [self._settings.mcp_servers[name] for name in names]
        cfg = cfgs[0]
        limit = max(c.timeout for c in cfgs) if timeout is None else timeout
        for name in names:
            self._pending.add(name)

        def _record(reason: str) -> None:
            for name in names:
                self._last_errors[name] = reason

        try:
            task = asyncio.create_task(
                self._start_endpoint(endpoint, cfg, limit), name=f"mcp-start-{cfg.url}"
            )
            try:
                await asyncio.wait_for(task, timeout=limit)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling() > 0:
                    # Cancelled from outside (shutdown, test teardown) — propagate.
                    task.cancel()
                    raise
                _record("przerwane podczas startu")
            except TimeoutError:
                _record(f"timeout startu ({limit:.0f}s)")
            except BaseException as exc:
                logger.exception("MCP endpoint connection failed: %s", cfg.url)
                _record(exc.__class__.__name__)
            else:
                _, session = self._endpoints[endpoint]
                for name in names:
                    self._last_errors.pop(name, None)
                    self._sessions[name] = session
        finally:
            for name in names:
                self._pending.discard(name)

    async def _start_endpoint(
        self, endpoint: EndpointKey, cfg: MCPServerConfig, limit: float
    ) -> None:
        if cfg.transport == "streamable_http":
            if "http_client" in _streamable_http_params() and httpx2 is not None:
                owned_client = httpx2.AsyncClient(
                    headers=cfg.headers, timeout=httpx2.Timeout(limit)
                )
                self._owned_clients[endpoint] = owned_client
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
            await self._teardown_endpoint(endpoint, client_cm, session if session_entered else None)
            raise
        self._endpoints[endpoint] = (client_cm, session)

    async def _teardown_endpoint(
        self, endpoint: EndpointKey, client_cm: Any, session: Any | None
    ) -> None:
        url = endpoint[1]
        if session is not None:
            try:
                await session.__aexit__(None, None, None)
            except Exception:
                logger.debug("MCP session close failed: %s", url, exc_info=True)
        try:
            await client_cm.__aexit__(None, None, None)
        except Exception:
            logger.debug("MCP client close failed: %s", url, exc_info=True)
        owned_client = self._owned_clients.pop(endpoint, None)
        if owned_client is not None:
            try:
                await owned_client.aclose()
            except Exception:
                logger.debug("MCP http client close failed: %s", url, exc_info=True)

    async def stop(self) -> None:
        """Close every session (idempotent — safe to call twice)."""
        for endpoint, (client_cm, session) in list(self._endpoints.items()):
            await self._teardown_endpoint(endpoint, client_cm, session)
            self._endpoints.pop(endpoint, None)
        # Every name shared its endpoint's session; drop all name entries.
        self._sessions.clear()
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
            # The server's own error text (e.g. a pydantic schema rejection) is
            # part of the message — otherwise rejections are invisible upstream.
            raise ToolCallError(f"Narzędzie zwróciło błąd: {server}/{tool}{_error_detail(result)}")
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
