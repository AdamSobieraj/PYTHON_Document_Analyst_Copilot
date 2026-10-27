"""End-to-end workflow runs over fakes: happy path, no sources, LLM outage, critic loop."""

from __future__ import annotations

from typing import Any

import pytest

from iso_insight.adapters.tracing.noop import NoOpTracing
from iso_insight.config import Settings
from iso_insight.pipeline.context import PipelineContext
from iso_insight.pipeline.stages import RECURSION_LIMIT, build_workflow
from iso_insight.schemas.workflow import WorkflowState
from tests.fakes.llm import FakeLLM
from tests.fakes.tools import FakeToolGateway

_QUERY = "Jakie są wymagania ISO 20022 dla wiadomości pacs.002?"


def _initial_state() -> WorkflowState:
    return {"query": _QUERY, "user_id": "tester"}


def _run_config() -> dict[str, Any]:
    return {"configurable": {"thread_id": "e2e-thread"}, "recursion_limit": RECURSION_LIMIT}


async def _run(settings: Settings, llm: FakeLLM, tools: FakeToolGateway) -> dict[str, Any]:
    context = PipelineContext(settings=settings, llm=llm, tools=tools, tracing=NoOpTracing())
    graph = build_workflow(context)
    result: dict[str, Any] = await graph.ainvoke(_initial_state(), _run_config())
    return result


def _events(result: dict[str, Any]) -> list[str]:
    return [entry["event"] for entry in result["audit_log"]]


async def test_happy_path_completes_with_golden_responses(
    settings: Settings, fake_llm: FakeLLM, fake_tools: FakeToolGateway
) -> None:
    result = await _run(settings, fake_llm, fake_tools)
    assert [call["prompt"] for call in fake_llm.json_calls] == [
        "iso_parser",
        "gap_analysis",
        "impact_risk",
        "draft_answer",
        "reflector",
    ]
    assert _events(result) == [
        "orchestrator_planned",
        "iso_parser_completed",
        "retrieval_completed",
        "evidence_validated",
        "gap_analysis_completed",
        "impact_risk_completed",
        "draft_generated",
        "reflector_completed",
        "output_validated",
        "confidence_checked",
        "workflow_completed",
    ]
    assert result["detected_standards"] == ["ISO20022"]
    assert result["selected_rag_servers"] == ["rag_default", "rag_iso20022"]
    assert len(result["sources"]) == 4
    assert result["confidence"] == pytest.approx(0.95)
    assert result["audit_status"] == "ok"
    final = result["final_answer"]
    assert "ISO 20022 definiuje" in final
    assert "Źródła:" in final
    assert "Zastrzeżenia:" in final


async def test_no_sources_escalates_after_budget(
    settings: Settings, golden_responses: dict[str, Any]
) -> None:
    llm = FakeLLM(responses=dict(golden_responses))
    tools = FakeToolGateway(
        sessions={"rag_default": [], "rag_iso20022": [], "jira": [], "confluence": []}
    )
    result = await _run(settings, llm, tools)
    assert result["iteration"] == 3
    assert result["confidence"] == pytest.approx(0.2)
    assert result["audit_status"] == "escalation_required"
    assert result["final_answer"].startswith("Niska pewność")
    events = _events(result)
    assert "workflow_escalated" in events
    assert "llm_error" not in events
    # iso + 3×(gap, impact, draft) + 2×improve; the reflector short-circuits
    # (no sources) so it never calls the LLM.
    assert len(llm.json_calls) == 12


async def test_llm_outage_degrades_but_completes(settings: Settings) -> None:
    llm = FakeLLM(fail_all=True)
    tools = FakeToolGateway(
        sessions={
            "rag_default": [
                {"quote": "ISO 20022 wymaga walidacji schematów XML.", "id": "rag-1"},
                {"quote": "pacs.002 przenosi polecenia płatności.", "id": "rag-2"},
            ],
            "rag_iso20022": [],
            "jira": [],
            "confluence": [],
        }
    )
    result = await _run(settings, llm, tools)
    # iso + 2×(gap, impact, draft, reflector, improve) + (gap, impact, draft, reflector)
    assert len(llm.json_calls) == 15
    errors = [entry for entry in result["audit_log"] if entry["event"] == "llm_error"]
    assert len(errors) == 15
    assert result["audit_status"] == "ok"
    assert result["confidence"] == pytest.approx(0.75)
    assert "Nie udało się wygenerować odpowiedzi przez LLM." in result["final_answer"]
    assert "workflow_completed" in _events(result)


async def test_reflector_fail_triggers_improve_loop(
    settings: Settings, golden_responses: dict[str, Any], fake_tools: FakeToolGateway
) -> None:
    fail = {"verdict": "fail", "feedback": "Odpowiedź wymaga uzupełnienia źródeł."}
    passed = {"verdict": "pass", "feedback": "Odpowiedź jest zgodna z źródłami."}
    llm = FakeLLM(responses=dict(golden_responses), sequences={"reflector": [fail, passed]})
    result = await _run(settings, llm, fake_tools)
    assert len(llm.json_calls) == 10
    assert result["iteration"] == 2
    assert result["critic_verdict"] == "pass"
    assert result["audit_status"] == "ok"
    refined = "ISO 20022 pacs.002 walidacja schematu wersja 2013"
    assert result["retrieval_query"] == refined
    # Iteration 2 retrieval (calls 4-7) must use the refined query.
    assert fake_tools.calls[4]["arguments"]["query"] == refined
    reflections = [
        entry for entry in result["audit_log"] if entry["event"] == "reflector_completed"
    ]
    assert len(reflections) == 2
    assert reflections[0]["verdict"] == "fail"
    assert reflections[1]["verdict"] == "pass"
