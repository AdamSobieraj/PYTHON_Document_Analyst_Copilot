"""Agent nodes: helpers, per-node behaviour, routing and audit events."""

from __future__ import annotations

from typing import Any

import pytest

from iso_insight.agents.nodes import (
    NodeFunctions,
    _clean_list,
    _current_query,
    _infer_standards,
    _safe_search,
)
from iso_insight.config import Settings
from iso_insight.exceptions import ToolCallError
from iso_insight.schemas.workflow import SourceRef, WorkflowState
from tests.fakes.llm import FakeLLM
from tests.fakes.tools import FakeToolGateway
from tests.fakes.tracing import FakeTracing


def _source(index: int, quote: str) -> SourceRef:
    return {
        "source_type": "rag_default",
        "source_id": f"src-{index}",
        "title": f"Źródło {index}",
        "url": None,
        "section": None,
        "quote": quote,
        "score": 0.9,
    }


def _sources(count: int) -> list[SourceRef]:
    return [_source(i, f"Cytat {i}.") for i in range(1, count + 1)]


@pytest.fixture
def nodes(
    settings: Settings,
    fake_llm: FakeLLM,
    fake_tools: FakeToolGateway,
    fake_tracing: FakeTracing,
) -> NodeFunctions:
    return NodeFunctions(settings, fake_llm, fake_tools, fake_tracing)


# ── helpers ───────────────────────────────────────────────────────────────


def test_clean_list_coerces_llm_values() -> None:
    assert _clean_list(None) == []
    assert _clean_list("pojedynczy tekst") == ["pojedynczy tekst"]
    assert _clean_list("   ") == []
    assert _clean_list(["a", " b ", "", "  "]) == ["a", "b"]
    assert _clean_list(42) == ["42"]


def test_infer_standards_heuristics() -> None:
    assert _infer_standards("ISO 8583 dla autoryzacji") == ["ISO8583"]
    assert _infer_standards("pacs.002 wiadomość") == ["ISO20022"]
    assert _infer_standards("EMV chip") == ["ISO8583"]
    assert _infer_standards("ISO15022 SWIFT") == ["ISO15022"]
    assert _infer_standards("ISO 20022 i ISO 8583") == ["ISO20022", "ISO8583"]
    assert _infer_standards("jak to działa") == []


def test_current_query_precedence() -> None:
    state: WorkflowState = {
        "query": "bazowe",
        "original_query": "oryginalne",
        "retrieval_query": "udoskonalone",
    }
    assert _current_query(state) == "udoskonalone"
    del state["retrieval_query"]
    assert _current_query(state) == "oryginalne"
    del state["original_query"]
    assert _current_query(state) == "bazowe"
    assert _current_query({}) == ""


async def test_safe_search_converts_failure_to_audit_event() -> None:
    state: WorkflowState = {}

    async def boom(state: WorkflowState, server: str) -> list[SourceRef]:
        raise ToolCallError("serwer niedostępny")

    result = await _safe_search(boom, state, "rag_default", "rag_default")
    assert result == []
    assert state["audit_log"][-1] == {
        "event": "mcp_tool_error",
        "tool": "rag_default",
        "args": ["rag_default"],
        "error": "serwer niedostępny",
    }


# ── routing ───────────────────────────────────────────────────────────────


def test_route_after_reflector_pass_and_budget(nodes: NodeFunctions) -> None:
    state: WorkflowState = {"iteration": 1, "critic_verdict": "pass"}
    assert nodes.route_after_reflector(state) == "output"
    state = {"iteration": 1, "critic_verdict": "fail"}
    assert nodes.route_after_reflector(state) == "improve"
    # Budget spent (max_iterations == 3) forces output even on a fail.
    state = {"iteration": 3, "critic_verdict": "fail"}
    assert nodes.route_after_reflector(state) == "output"


def test_route_after_confidence(nodes: NodeFunctions) -> None:
    state: WorkflowState = {"confidence": 0.95}
    assert nodes.route_after_confidence(state) == "final"
    state = {"confidence": 0.2}
    assert nodes.route_after_confidence(state) == "escalate"


# ── nodes ─────────────────────────────────────────────────────────────────


def test_orchestrator_resets_stale_run_state(nodes: NodeFunctions) -> None:
    state: WorkflowState = {
        "query": "nowe pytanie",
        "detected_standards": ["ISO8583"],
        "sources": _sources(1),
        "iso_requirements": ["stare"],
        "gaps": ["stara luka"],
        "draft_answer": "stary draft",
        "critic_verdict": "pass",
        "confidence": 0.9,
        "iteration": 2,
        "audit_log": [{"event": "poprzedni_bieg"}],
    }
    result: Any = nodes.orchestrator(state)
    assert result["detected_standards"] == []
    assert result["sources"] == []
    assert result["iso_requirements"] == []
    assert result["gaps"] == []
    assert result["draft_answer"] == ""
    assert result["critic_verdict"] is None
    assert result["confidence"] == 0.0
    assert result["iteration"] == 0
    assert result["original_query"] == "nowe pytanie"
    assert result["retrieval_query"] == "nowe pytanie"
    # Thread-level audit trail is preserved.
    assert result["audit_log"][0] == {"event": "poprzedni_bieg"}
    assert result["audit_log"][-1] == {
        "event": "orchestrator_planned",
        "original_query": "nowe pytanie",
        "retrieval_query": "nowe pytanie",
    }


async def test_iso_parser_uses_llm_golden(nodes: NodeFunctions) -> None:
    state: WorkflowState = {"query": "Jakie są wymagania ISO 20022 dla wiadomości pacs.002?"}
    result: Any = await nodes.iso_parser(state)
    assert result["detected_standards"] == ["ISO20022"]
    assert len(result["iso_requirements"]) == 2
    event = result["audit_log"][-1]
    assert event["event"] == "iso_parser_completed"
    assert event["llm_used"] is True


async def test_iso_parser_falls_back_to_heuristics_on_llm_error(
    settings: Settings,
    fake_tools: FakeToolGateway,
    fake_tracing: FakeTracing,
) -> None:
    local_nodes = NodeFunctions(settings, FakeLLM(fail_all=True), fake_tools, fake_tracing)
    state: WorkflowState = {"query": "Czy pacs.002 jest zgodny z ISO 20022?"}
    result: Any = await local_nodes.iso_parser(state)
    # "pacs" in the query drives the heuristic fallback.
    assert result["detected_standards"] == ["ISO20022"]
    assert len(result["iso_requirements"]) == 1  # deterministic fallback line
    events = [entry["event"] for entry in result["audit_log"]]
    assert "llm_error" in events
    assert result["audit_log"][-1]["llm_used"] is False


async def test_retrieval_collects_and_dedupes_sources(nodes: NodeFunctions) -> None:
    state: WorkflowState = {
        "query": "Jakie są wymagania ISO 20022 dla wiadomości pacs.002?",
        "detected_standards": ["ISO20022"],
    }
    result: Any = await nodes.retrieval(state)
    assert result["selected_rag_servers"] == ["rag_default", "rag_iso20022"]
    assert len(result["sources"]) == 4
    assert result["rag_context"] == [source["quote"] for source in result["sources"]]
    assert result["audit_log"][-1]["event"] == "retrieval_completed"


def test_evidence_validator_drops_quoteless_sources_and_scales_confidence(
    nodes: NodeFunctions,
) -> None:
    state: WorkflowState = {"sources": _sources(4)}
    result: Any = nodes.evidence_validator(state)
    assert len(result["sources"]) == 4
    assert result["confidence"] == pytest.approx(0.95)
    event = result["audit_log"][-1]
    assert event["event"] == "evidence_validated"
    assert event["source_count"] == 4

    state = {"sources": _sources(2) + [_source(3, "")]}
    result = nodes.evidence_validator(state)
    assert len(result["sources"]) == 2
    assert result["confidence"] == pytest.approx(0.75)

    state = {"sources": []}
    result = nodes.evidence_validator(state)
    assert result["confidence"] == pytest.approx(0.25)


async def test_reflector_short_circuits_without_sources(
    nodes: NodeFunctions, fake_llm: FakeLLM
) -> None:
    state: WorkflowState = {"draft_answer": "Odpowiedź.", "sources": [], "iteration": 0}
    result: Any = await nodes.reflector(state)
    assert result["critic_verdict"] == "fail"
    assert result["iteration"] == 1
    assert result["audit_log"][-1] == {
        "event": "reflector_completed",
        "verdict": "fail",
        "iteration": 1,
        "llm_used": False,
    }
    assert fake_llm.json_calls == []  # no LLM call on short-circuit


async def test_reflector_pass_with_sources(nodes: NodeFunctions) -> None:
    state: WorkflowState = {
        "draft_answer": "Odpowiedź.",
        "sources": _sources(1),
        "iteration": 0,
    }
    result: Any = await nodes.reflector(state)
    assert result["critic_verdict"] == "pass"
    assert result["iteration"] == 1
    event = result["audit_log"][-1]
    assert event["verdict"] == "pass"
    assert event["llm_used"] is True


def test_output_validator_ok(nodes: NodeFunctions) -> None:
    state: WorkflowState = {
        "draft_answer": "Sformułowana odpowiedź.",
        "draft_caveats": ["Zastrzeżenie A"],
        "sources": _sources(2),
    }
    result: Any = nodes.output_validator(state)
    assert result["audit_status"] == "ok"
    final = result["final_answer"]
    assert final.startswith("Sformułowana odpowiedź.")
    assert "Źródła:" in final
    assert "Zastrzeżenia:" in final
    assert "- Źródło 1 (rag_default): Cytat 1." in final
    assert result["audit_log"][-1]["event"] == "output_validated"


def test_output_validator_missing_sources(nodes: NodeFunctions) -> None:
    state: WorkflowState = {
        "draft_answer": "Odpowiedź.",
        "sources": [],
        "confidence": 0.8,
    }
    result: Any = nodes.output_validator(state)
    assert result["audit_status"] == "missing_sources"
    assert result["final_answer"].startswith(
        "Nie mogę potwierdzić odpowiedzi bez wystarczających źródeł"
    )
    assert result["confidence"] == pytest.approx(0.2)  # min(0.8, 0.2)


def test_output_validator_missing_draft(nodes: NodeFunctions) -> None:
    state: WorkflowState = {"draft_answer": "   ", "sources": _sources(1)}
    result: Any = nodes.output_validator(state)
    assert result["audit_status"] == "draft_missing"
    assert result["final_answer"] == "Nie udało się wygenerować odpowiedzi."
    assert result["confidence"] == 0.0


def test_confidence_check_tags_status(nodes: NodeFunctions) -> None:
    state: WorkflowState = {"confidence": 0.9, "audit_status": "ok"}
    result: Any = nodes.confidence_check(state)
    assert result["audit_status"] == "ok"
    assert result["audit_log"][-1]["event"] == "confidence_checked"

    state = {"confidence": 0.9}
    result = nodes.confidence_check(state)
    assert result["audit_status"] == "high_confidence"

    state = {"confidence": 0.3, "audit_status": "ok"}
    result = nodes.confidence_check(state)
    assert result["audit_status"] == "low_confidence"


def test_final_answer_applies_defaults(nodes: NodeFunctions) -> None:
    state: WorkflowState = {"final_answer": "Gotowa odpowiedź.", "audit_status": ""}
    result: Any = nodes.final_answer(state)
    assert result["audit_status"] == "completed"
    assert result["audit_log"][-1]["event"] == "workflow_completed"

    state = {"audit_status": "ok"}
    result = nodes.final_answer(state)
    assert result["final_answer"] == "Brak odpowiedzi."
    assert result["audit_status"] == "ok"


def test_escalate_replaces_answer(nodes: NodeFunctions) -> None:
    state: WorkflowState = {"final_answer": "stara odpowiedź", "confidence": 0.1}
    result: Any = nodes.escalate(state)
    assert result["audit_status"] == "escalation_required"
    assert result["final_answer"].startswith("Niska pewność odpowiedzi")
    assert result["audit_log"][-1]["event"] == "workflow_escalated"
