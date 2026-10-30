"""The tools_gate node: no MCP server connected => deterministic fast answer.

The gate is the first node of the graph. With an empty tool gateway it makes
exactly one short reconnect probe (``mcp_probe_timeout``); if still nothing is
connected, the run ends there with the deterministic "service unavailable"
answer — zero LLM calls.
"""

from __future__ import annotations

from iso_insight.agents.nodes import (
    TOOLS_UNAVAILABLE_ANSWER,
    TOOLS_UNAVAILABLE_STATUS,
    NodeFunctions,
)
from iso_insight.config import Settings
from iso_insight.schemas.workflow import WorkflowState
from tests.fakes.llm import FakeLLM
from tests.fakes.tools import FakeToolGateway
from tests.fakes.tracing import FakeTracing

_QUERY = "Jakie są wymagania ISO 20022 dla wiadomości pacs.002?"


def _nodes(settings: Settings, tools: FakeToolGateway) -> tuple[NodeFunctions, FakeLLM]:
    llm = FakeLLM()
    return NodeFunctions(settings, llm, tools, FakeTracing()), llm


async def test_gate_passes_when_a_server_is_connected(
    settings: Settings, fake_tools: FakeToolGateway
) -> None:
    nodes, llm = _nodes(settings, fake_tools)
    result: WorkflowState = await nodes.tools_gate({"query": _QUERY})
    assert result["tools_available"] is True
    assert result["audit_log"][-1]["event"] == "tools_gate_passed"
    assert fake_tools.refresh_calls == []  # nothing to probe: a server is up
    assert llm.json_calls == []


async def test_gate_short_circuits_with_deterministic_answer(settings: Settings) -> None:
    tools = FakeToolGateway(sessions={})
    tools.startup_errors = ["rag_default: timeout startu (30s)"]
    nodes, llm = _nodes(settings, tools)
    result: WorkflowState = await nodes.tools_gate({"query": _QUERY})
    assert result["tools_available"] is False
    assert result["final_answer"] == TOOLS_UNAVAILABLE_ANSWER
    assert result["audit_status"] == TOOLS_UNAVAILABLE_STATUS
    assert result["confidence"] == 0.0
    assert result["sources"] == []
    assert result["audit_log"][-1] == {
        "event": "tools_unavailable",
        "startup_errors": ["rag_default: timeout startu (30s)"],
    }
    # exactly one short probe, bounded by the configured timeout, zero LLM
    assert tools.refresh_calls == [settings.mcp_probe_timeout]
    assert llm.json_calls == []


async def test_gate_probe_picks_up_a_server_that_just_came_up(settings: Settings) -> None:
    tools = FakeToolGateway(sessions={})
    tools.on_refresh = lambda: tools.sessions.setdefault("rag_default", [])
    nodes, llm = _nodes(settings, tools)
    result: WorkflowState = await nodes.tools_gate({"query": _QUERY})
    assert result["tools_available"] is True
    assert result["audit_log"][-1]["event"] == "tools_gate_passed"
    assert tools.refresh_calls == [settings.mcp_probe_timeout]
    assert llm.json_calls == []


def test_route_after_tools_gate(settings: Settings, fake_tools: FakeToolGateway) -> None:
    nodes, _ = _nodes(settings, fake_tools)
    assert nodes.route_after_tools_gate({"tools_available": True}) == "run"
    assert nodes.route_after_tools_gate({"tools_available": False}) == "done"
    assert nodes.route_after_tools_gate({}) == "done"
