"""MCP tool gateway adapter: error surfacing and per-endpoint session dedup.

The transport (``mcp`` SDK) is stubbed at the module level, so these tests
run without any MCP server. The contract suite (``tests/contracts/test_tools.py``)
covers the fake and real gateways together.
"""

from __future__ import annotations

from typing import Any

import pytest

from iso_insight.adapters.tools import mcp as mcp_module
from iso_insight.adapters.tools.mcp import MCPToolGateway
from iso_insight.config import Settings
from iso_insight.exceptions import ToolCallError

RAG_URL = "http://shared:8011/mcp"
WIKI_URL = "http://wiki:9002/mcp"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _ToolResult:
    """Minimal stand-in for the SDK's tool result (field spellings: mcp 1.x/2.x)."""

    def __init__(self, *, is_error: bool = False, text: str = "", structured: Any = None) -> None:
        self.isError = is_error
        self.structured_content = structured
        self.content = [{"type": "text", "text": text}] if text else []


class _SessionStub:
    """A ready-made session injected straight into the gateway's session map."""

    def __init__(self, result: _ToolResult) -> None:
        self._result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> _ToolResult:
        self.calls.append((tool, arguments))
        return self._result


class _FakeClientSession:
    """Stands in for ``mcp.ClientSession``; the URL arrives as the ``read`` stream."""

    def __init__(self, env: _FakeMCP, read: str, write: Any) -> None:
        self._url = read
        self._env = env
        self.closed = False

    async def __aenter__(self) -> _FakeClientSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def initialize(self) -> None:
        if self._url in self._env.fail_urls:
            raise ConnectionRefusedError(f"connection refused: {self._url}")

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> Any:
        return None


class _FakeClientCM:
    """Stands in for the ``streamable_http_client`` context manager."""

    def __init__(self, env: _FakeMCP, url: str) -> None:
        self._env = env
        self._url = url
        self.closed = False

    async def __aenter__(self) -> tuple[str, None]:
        # The gateway unpacks (read, write); the fake session reads the URL
        # from ``read`` so it knows which endpoint it belongs to.
        return self._url, None

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True


class _FakeMCP:
    """Registry of what the stubbed SDK did (connections, sessions)."""

    def __init__(self, fail_urls: frozenset[str] = frozenset()) -> None:
        self.fail_urls = fail_urls
        self.connects: list[str] = []
        self.sessions: list[_FakeClientSession] = []


@pytest.fixture
def fake_mcp(monkeypatch: pytest.MonkeyPatch) -> _FakeMCP:
    """Stub the SDK symbols inside the gateway module (deterministic, offline)."""
    env = _FakeMCP()

    def _session(read: str, write: Any) -> _FakeClientSession:
        session = _FakeClientSession(env, read, write)
        env.sessions.append(session)
        return session

    def _factory(
        url: str, headers: dict[str, str] | None = None, timeout: float | None = None
    ) -> _FakeClientCM:
        env.connects.append(url)
        return _FakeClientCM(env, url)

    monkeypatch.setattr(mcp_module, "ClientSession", _session)
    monkeypatch.setattr(mcp_module, "streamable_http_client", _factory)
    monkeypatch.setattr(mcp_module, "sse_client", _factory)
    monkeypatch.setattr(mcp_module, "httpx2", None)  # force the 1.x client path
    return env


@pytest.fixture
def shared_endpoint_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Three RAG names + two wiki names on two shared endpoints (like the local .env)."""
    for prefix in ("RAG_DEFAULT", "RAG_ISO20022", "RAG_ISO8583"):
        monkeypatch.setenv(f"{prefix}_MCP_URL", RAG_URL)
    for prefix in ("JIRA", "CONFLUENCE"):
        monkeypatch.setenv(f"{prefix}_MCP_URL", WIKI_URL)
    return Settings(_env_file=None)


# ---------------------------------------------------------------------------
# call(): the server's error text reaches the domain exception
# ---------------------------------------------------------------------------


async def test_call_includes_the_server_error_text(shared_endpoint_settings: Settings) -> None:
    gateway = MCPToolGateway(shared_endpoint_settings)
    session = _SessionStub(
        _ToolResult(
            is_error=True,
            text=(
                "1 validation error for query_knowledge_baseArguments\n"
                "collection_name: field required"
            ),
        )
    )
    gateway._sessions["rag_default"] = session
    with pytest.raises(ToolCallError) as excinfo:
        await gateway.call("rag_default", "query_knowledge_base", {"query": "co to ISO 20022?"})
    message = str(excinfo.value)
    assert "rag_default/query_knowledge_base" in message
    assert "collection_name: field required" in message


async def test_call_error_text_is_truncated(shared_endpoint_settings: Settings) -> None:
    gateway = MCPToolGateway(shared_endpoint_settings)
    session = _SessionStub(_ToolResult(is_error=True, text="x" * 1000))
    gateway._sessions["rag_default"] = session
    with pytest.raises(ToolCallError) as excinfo:
        await gateway.call("rag_default", "query_knowledge_base", {"query": "q"})
    message = str(excinfo.value)
    assert "x" * 1000 not in message
    assert len(message) <= 400
    assert message.endswith("…")


async def test_call_without_error_text_keeps_the_plain_message(
    shared_endpoint_settings: Settings,
) -> None:
    gateway = MCPToolGateway(shared_endpoint_settings)
    gateway._sessions["rag_default"] = _SessionStub(_ToolResult(is_error=True))
    with pytest.raises(ToolCallError) as excinfo:
        await gateway.call("rag_default", "query_knowledge_base", {"query": "q"})
    assert str(excinfo.value) == "Narzędzie zwróciło błąd: rag_default/query_knowledge_base"


async def test_call_success_returns_the_structured_payload(
    shared_endpoint_settings: Settings,
) -> None:
    structured = {"results": [{"quote": "ISO 20022 wymaga walidacji schematów XML.", "id": "1"}]}
    gateway = MCPToolGateway(shared_endpoint_settings)
    gateway._sessions["rag_default"] = _SessionStub(_ToolResult(structured=structured))
    payload = await gateway.call("rag_default", "query_knowledge_base", {"query": "q"})
    assert payload == structured


# ---------------------------------------------------------------------------
# start(): one physical session per endpoint, per-name status
# ---------------------------------------------------------------------------


async def test_start_opens_one_session_per_shared_endpoint(
    fake_mcp: _FakeMCP, shared_endpoint_settings: Settings
) -> None:
    gateway = MCPToolGateway(shared_endpoint_settings)
    await gateway.start()
    try:
        # two endpoints → two physical connections, not five
        assert fake_mcp.connects.count(RAG_URL) == 1
        assert fake_mcp.connects.count(WIKI_URL) == 1
        # all five logical names are connected
        assert gateway.status().connected == sorted(shared_endpoint_settings.mcp_servers)
        # the names on one endpoint share the same session object
        assert (
            gateway._sessions["rag_default"] is gateway._sessions["rag_iso20022"]
            is gateway._sessions["rag_iso8583"]
        )
        assert gateway._sessions["jira"] is gateway._sessions["confluence"]
        assert gateway._sessions["jira"] is not gateway._sessions["rag_default"]
    finally:
        await gateway.stop()


async def test_failed_endpoint_records_an_error_for_every_name(
    fake_mcp: _FakeMCP, shared_endpoint_settings: Settings
) -> None:
    fake_mcp.fail_urls = frozenset({WIKI_URL})
    gateway = MCPToolGateway(shared_endpoint_settings)
    await gateway.start()
    try:
        status = gateway.status()
        assert sorted(status.connected) == ["rag_default", "rag_iso20022", "rag_iso8583"]
        # the dead endpoint costs one connection attempt, and every name on it
        # carries the recorded error (accounting invariant)
        assert fake_mcp.connects.count(WIKI_URL) == 1
        errors = " | ".join(status.startup_errors)
        assert "jira: ConnectionRefusedError" in errors
        assert "confluence: ConnectionRefusedError" in errors
    finally:
        await gateway.stop()


async def test_stop_closes_every_endpoint_session_once(
    fake_mcp: _FakeMCP, shared_endpoint_settings: Settings
) -> None:
    gateway = MCPToolGateway(shared_endpoint_settings)
    await gateway.start()
    await gateway.stop()
    await gateway.stop()  # idempotent
    assert len(fake_mcp.sessions) == 2  # one session per endpoint, not one per name
    assert all(session.closed for session in fake_mcp.sessions)
