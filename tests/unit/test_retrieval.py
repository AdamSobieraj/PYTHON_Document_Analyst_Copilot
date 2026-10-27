"""Retrieval: server selection, payload normalisation, call arguments."""

from __future__ import annotations

from typing import Any

from iso_insight.config import Settings
from iso_insight.retrieval.search import (
    _normalize_sources,
    confluence_search,
    jira_search,
    rag_search,
    select_rag_servers,
)
from iso_insight.schemas.workflow import WorkflowState
from tests.fakes.tools import FakeToolGateway


def test_select_rag_servers_adds_standard_specific_bases(settings: Settings) -> None:
    assert select_rag_servers(["ISO20022"], settings) == ["rag_default", "rag_iso20022"]
    assert select_rag_servers(["ISO8583"], settings) == ["rag_default", "rag_iso8583"]
    # Unmapped and unknown standards only ever add the default base.
    assert select_rag_servers(["ISO15022"], settings) == ["rag_default"]
    assert select_rag_servers([], settings) == ["rag_default"]
    assert select_rag_servers(["ISO20022", "ISO8583", "ISO20022"], settings) == [
        "rag_default",
        "rag_iso20022",
        "rag_iso8583",
    ]


def test_normalize_sources_maps_heterogeneous_items() -> None:
    payload = {
        "results": [
            {
                "quote": "pełny cytat",
                "id": "doc-1",
                "title": "Dokument 1",
                "url": "https://example.com/1",
                "score": 0.9,
            },
            {"snippet": "z snippetu", "relevance": 0.5},
            {"text": "z pola text"},
            {"body": "z pola body", "path": "rozdział 3"},
            {"title": "bez cytatu — pomijany"},
            "surowy łańcuch",
        ]
    }
    sources = _normalize_sources(payload, "rag")
    assert [source["source_id"] for source in sources] == [
        "doc-1",
        "rag-2",
        "rag-3",
        "rag-4",
        "rag-6",
    ]
    assert sources[0]["quote"] == "pełny cytat"
    assert sources[0]["source_type"] == "rag"
    assert sources[0]["score"] == 0.9
    assert sources[1]["quote"] == "z snippetu"
    assert sources[1]["title"] == "rag source 2"
    assert sources[1]["score"] == 0.5  # from "relevance"
    assert sources[3]["section"] == "rozdział 3"
    assert sources[4] == {
        "source_type": "rag",
        "source_id": "rag-6",
        "title": "rag source 6",
        "url": None,
        "section": None,
        "quote": "surowy łańcuch",
        "score": 0.0,
    }


def test_normalize_sources_payload_shape_fallbacks() -> None:
    # List payload, "items" key, plain dict with a quote.
    assert _normalize_sources(["a", "b"], "rag")[0]["quote"] == "a"
    assert len(_normalize_sources({"items": [{"quote": "x"}]}, "rag")) == 1
    single = _normalize_sources({"quote": "pojedynczy obiekt", "id": "one"}, "rag")
    assert single[0]["source_id"] == "one"
    # No quote anywhere → no sources; None → no sources.
    assert _normalize_sources({"title": "bez cytatu"}, "rag") == []
    assert _normalize_sources(None, "rag") == []


async def test_rag_search_unknown_server_yields_no_call(settings: Settings) -> None:
    tools = FakeToolGateway()
    state: WorkflowState = {"query": "ISO 20022"}
    sources = await rag_search(state, tools, settings, server_name="nie_zakonfigurowany")
    assert sources == []
    assert tools.calls == []


async def test_search_arguments_use_per_server_result_keys(settings: Settings) -> None:
    state: WorkflowState = {
        "query": "oryginalne pytanie",
        "detected_standards": ["ISO20022"],
        "user_id": "tester",
        "permissions": ["read_iso"],
    }
    tools = FakeToolGateway(sessions={"rag_default": [], "jira": [], "confluence": []})
    await rag_search(state, tools, settings)
    await jira_search(state, tools, settings)
    await confluence_search(state, tools, settings)
    rag_args, jira_args, confluence_args = (call["arguments"] for call in tools.calls)
    assert rag_args["top_k"] == 5  # default MCPServerConfig.top_k
    assert "max_results" not in rag_args
    assert jira_args["max_results"] == 5
    assert confluence_args["max_results"] == 5
    for args in (rag_args, jira_args, confluence_args):
        assert args["query"] == "oryginalne pytanie"
        assert args["standards"] == ["ISO20022"]
        assert args["user_id"] == "tester"
        assert args["permissions"] == ["read_iso"]


async def test_retrieval_query_takes_precedence_over_original(settings: Settings) -> None:
    state: WorkflowState = {
        "query": "oryginalne pytanie",
        "retrieval_query": "udoskonalone pytanie",
    }
    tools = FakeToolGateway(sessions={"rag_default": []})
    await rag_search(state, tools, settings)
    assert tools.calls[0]["arguments"]["query"] == "udoskonalone pytanie"


async def test_rag_search_normalizes_server_payload(settings: Settings) -> None:
    payload: dict[str, Any] = {
        "results": [{"quote": "cytat z serwera", "id": "srv-1", "title": "Tytuł"}]
    }
    tools = FakeToolGateway(sessions={"rag_default": payload})
    state: WorkflowState = {"query": "ISO 20022"}
    sources = await rag_search(state, tools, settings)
    assert [source["source_id"] for source in sources] == ["srv-1"]
    assert sources[0]["source_type"] == "rag_default"
