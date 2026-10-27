"""Tool gateway port contract: fake and MCP gateway share the guarantees.

One server failing never takes the others down: startup errors are
collected, a call without a session is the domain ``ToolCallError``, and
teardown is idempotent (CLAUDE.md: one contract suite per port).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from iso_insight.adapters.tools.mcp import MCPToolGateway
from iso_insight.config import Settings
from iso_insight.exceptions import ToolCallError
from iso_insight.ports.tools import ToolGateway
from iso_insight.schemas.mcp import ToolStatus
from tests.fakes.tools import FakeToolGateway


@pytest.fixture(params=["fake", "mcp"])
async def gateway(request: pytest.FixtureRequest, settings: Settings) -> AsyncIterator[ToolGateway]:
    if request.param == "fake":
        current: ToolGateway = FakeToolGateway(sessions={})
    else:
        current = MCPToolGateway(settings)
    yield current
    await current.stop()


async def test_start_never_raises_and_status_is_well_formed(gateway: ToolGateway) -> None:
    await gateway.start()
    status = gateway.status()
    assert isinstance(status, ToolStatus)
    assert isinstance(status.connected, list)
    assert isinstance(status.startup_errors, list)


async def test_call_without_session_raises_tool_call_error(gateway: ToolGateway) -> None:
    await gateway.start()
    if gateway.status().connected:
        pytest.skip("serwer MCP dostępny — ścieżka braku sesji nie jest odtwarzalna")
    with pytest.raises(ToolCallError):
        await gateway.call("rag_default", "rag_search", {"query": "ISO 20022"})


async def test_stop_is_idempotent(gateway: ToolGateway) -> None:
    await gateway.start()
    await gateway.stop()
    await gateway.stop()


async def test_mcp_accounting_covers_every_configured_server(settings: Settings) -> None:
    # Environment-independent MCP guarantee: every configured server ends up
    # either connected or in startup_errors — never silently dropped.
    gateway = MCPToolGateway(settings)
    await gateway.start()
    try:
        status = gateway.status()
        configured = set(settings.mcp_servers)
        assert set(status.connected) <= configured
        assert len(status.connected) + len(status.startup_errors) == len(configured)
    finally:
        await gateway.stop()
