"""MCP (Model Context Protocol) client manager.

Maintains one ClientSession per configured MCP server (RAG bases, Jira,
Confluence) and exposes call_tool() for the search wrappers in tools.py.

Supported transports: streamable HTTP and SSE. Compatible with both the
mcp SDK 1.x and 2.x lines (different client names/signatures and result
field casing) — see the import fallbacks below.

Design goals:
- one unreachable server must not break app startup: failures are
  collected in startup_errors and surfaced via MCPManager.status(),
- no resource leaks on partial startup (session, transport context and
  httpx client are always cleaned up),
- tool results are normalized: structured content is preferred,
  otherwise the text blocks are concatenated and JSON-decoded when
  possible.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Dict, List, Optional, Tuple

from mcp import ClientSession

try:
    from mcp.client.streamable_http import streamable_http_client
except Exception:
    try:
        # mcp 1.x used the name without underscore
        from mcp.client.streamable_http import (
            streamablehttp_client as streamable_http_client,
        )
    except Exception:
        streamable_http_client = None  # type: ignore[assignment]

try:
    from mcp.client.sse import sse_client
except Exception:
    sse_client = None

try:
    import httpx2
except Exception:
    httpx2 = None  # type: ignore[assignment]

from config import MCP_SERVERS


class MCPError(RuntimeError):
    """Raised for any MCP-level failure (connection, transport, tool call)."""


def _result_field(result: Any, *names: str) -> Any:
    """Read a CallToolResult field across mcp 1.x (camelCase) and 2.x (snake_case)."""
    for name in names:
        value = getattr(result, name, None)
        if value is not None:
            return value
    return None


def _streamable_http_params() -> set:
    """Parameter names of the installed streamable HTTP client factory.

    Used to detect the SDK generation at runtime: 2.x takes an
    `http_client` argument, 1.x takes `headers`/`timeout` directly.
    """
    try:
        return set(inspect.signature(streamable_http_client).parameters)
    except Exception:
        return set()


def _parse_mcp_result(result: Any) -> Any:
    """Normalize a CallToolResult into plain Python data.

    Preference order: structured content, then concatenated text blocks
    (JSON-decoded when the combined text is valid JSON, otherwise kept
    as {"text": ...}).
    """
    structured = _result_field(result, "structured_content", "structuredContent")
    if structured is not None:
        return structured

    texts: List[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            texts.append(str(text))

    if not texts:
        return {}

    combined = "\n".join(texts)

    try:
        return json.loads(combined)
    except json.JSONDecodeError:
        return {"text": combined}


class MCPManager:
    """Owns MCP sessions for all configured servers and their lifecycle."""

    def __init__(self) -> None:
        """Create an empty manager; call start() before using call_tool().

        Keeps, per server: the initialized ClientSession, the (transport
        context manager, session) pair in insertion order, and — for mcp
        >= 2.x — the httpx client this manager created itself (the SDK
        will not close clients it did not create). stop() tears all of it
        down in reverse order, so a failed or partial startup leaks no
        connections or background tasks.
        """
        # server name -> initialized ClientSession
        self._sessions: Dict[str, ClientSession] = {}
        # (transport context manager, session) pairs, for ordered teardown
        self._contexts: List[Tuple[Any, Any]] = []
        # server name -> httpx2 client we created ourselves (mcp >= 2.x does
        # not close clients it did not create)
        self._owned_clients: Dict[str, Any] = {}
        # human-readable per-server startup failures (shown in /health)
        self._startup_errors: List[str] = []

    def status(self) -> Dict[str, Any]:
        """Connected servers and startup errors — returned by GET /health.

        "connected" lists the server names with a live session;
        "startup_errors" carries one human-readable string per failed
        server (URL, transport, token, TLS, ...), which is how operators
        see why a backend is missing while the API itself stays up.
        """
        return {
            "connected": list(self._sessions.keys()),
            "startup_errors": self._startup_errors,
        }

    async def start(self) -> None:
        """Start all configured servers; a single failure never aborts startup.

        Each failure is recorded in _startup_errors so the API can boot
        (and answer with graceful degradation) even when some backends
        are down.
        """
        self._startup_errors = []

        for server_name, cfg in MCP_SERVERS.items():
            try:
                await self._start_one(server_name, cfg)
            except asyncio.CancelledError:
                # SDK (mcp >= 2.x) surfaces transport failures as CancelledError,
                # which is a BaseException and would escape `except Exception`.
                # Re-raise only if it is a real cancellation of our own task
                # (e.g. app shutdown), otherwise treat as a startup error.
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                self._startup_errors.append(
                    f"{server_name}: przerwane podczas startu"
                )
            except BaseException as exc:
                self._startup_errors.append(f"{server_name}: {exc}")

    async def _start_one(self, server_name: str, cfg: Dict[str, Any]) -> None:
        """Open the transport, create and initialize the ClientSession.

        Raises MCPError (or the underlying error) on any failure; all
        partially-created resources are cleaned up before re-raising.
        """
        transport = str(cfg.get("transport", "streamable_http")).lower()
        url = cfg.get("url")
        headers = cfg.get("headers", {})
        timeout = float(cfg.get("timeout", 30))

        if not url:
            raise MCPError(f"Brak URL dla MCP serwera {server_name}.")

        # mcp >= 2.x accepts a pre-configured httpx2 client and does not close
        # clients it did not create itself, so we track them for cleanup.
        owned_client: Optional[Any] = None

        if transport in ("streamable_http", "streamable-http", "http"):
            if streamable_http_client is None:
                raise MCPError(
                    "Brak streamable HTTP clienta w zainstalowanej wersji mcp."
                )

            params = _streamable_http_params()

            if "http_client" in params:
                # mcp >= 2.x signature: (url, *, http_client, terminate_on_close)
                if httpx2 is None:
                    raise MCPError(
                        "Brak httpx2 wymaganego przez zainstalowaną wersję mcp."
                    )
                owned_client = httpx2.AsyncClient(
                    headers=headers,
                    timeout=httpx2.Timeout(timeout),
                )
                client_cm = streamable_http_client(url, http_client=owned_client)
            else:
                # mcp 1.x: url/headers/timeout as direct parameters
                client_cm = streamable_http_client(
                    url=url,
                    headers=headers,
                    timeout=timeout,
                )

        elif transport in ("sse", "sse-http", "event-stream"):
            if sse_client is None:
                raise MCPError("Brak SSE clienta w zainstalowanej wersji mcp.")

            client_cm = sse_client(
                url=url,
                headers=headers,
                timeout=timeout,
            )

        else:
            raise MCPError(f"Nieobsługiwany transport MCP: {transport}")

        session: Optional[ClientSession] = None
        cm_entered = False

        try:
            # Enter the transport context, then the session, then handshake.
            entered = await client_cm.__aenter__()
            cm_entered = True

            read_stream, write_stream = entered[0], entered[1]

            session = ClientSession(read_stream, write_stream)
            await session.__aenter__()
            await session.initialize()
        except BaseException:
            # Clean up the partial startup; otherwise transports and httpx
            # clients leak (tasks and open connections).
            if session is not None:
                try:
                    await session.__aexit__(None, None, None)
                except BaseException:
                    pass
            if cm_entered:
                try:
                    await client_cm.__aexit__(None, None, None)
                except BaseException:
                    pass
            if owned_client is not None:
                try:
                    await owned_client.aclose()
                except BaseException:
                    pass
            raise

        self._sessions[server_name] = session
        self._contexts.append((client_cm, session))
        if owned_client is not None:
            self._owned_clients[server_name] = owned_client

    async def stop(self) -> None:
        """Tear everything down in reverse order: sessions, transports, httpx clients.

        Idempotent — the context lists are cleared, so a second call is a
        no-op; both the API lifespan and the Celery worker shutdown path
        rely on this.
        """
        for client_cm, session in reversed(self._contexts):
            for cm in (session, client_cm):
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    pass

        for client in self._owned_clients.values():
            try:
                await client.aclose()
            except Exception:
                pass

        self._contexts.clear()
        self._sessions.clear()
        self._owned_clients.clear()

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Call one tool on a connected server and return normalized data.

        Raises MCPError when the server is not connected, the call times
        out, or the tool reports an error result.
        """
        if server not in self._sessions:
            raise MCPError(f"Serwer MCP nie jest połączony: {server}")

        session = self._sessions[server]
        arguments = arguments or {}

        try:
            if timeout is not None:
                result = await asyncio.wait_for(
                    session.call_tool(tool, arguments),
                    timeout,
                )
            else:
                result = await session.call_tool(tool, arguments)
        except asyncio.TimeoutError as exc:
            raise MCPError(f"Timeout MCP tool: {server}.{tool}") from exc

        # Tool-level error flag (mcp 1.x: isError, 2.x: is_error).
        if _result_field(result, "is_error", "isError"):
            raise MCPError(f"MCP tool zwrócił błąd: {server}.{tool}")

        return _parse_mcp_result(result)


# Process-wide singleton used by tools.py and the FastAPI lifespan.
mcp_manager = MCPManager()
