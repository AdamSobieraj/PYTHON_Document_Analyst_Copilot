"""Retrieval: server selection, payload normalisation, call arguments."""

from __future__ import annotations

from typing import Any

import pytest

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

# The native RAG server's (PYTHON-Agent-MCP) hit dump, verbatim shape:
# FastMCP wraps it both as a text block and as structured_content
# {"result": <dump>} — the structured form wins in the gateway, so the
# normaliser must parse it back into per-document items.
DUMP = """\
--- DOKUMENT 1 (Relewancja: 0.5943) ---
Tytuł: ISO20022_MDRPart1_PaymentsInitiation_2023_2024_v2.pdf
Źródło (plik): s3://mojsuperbucket/general/ISO20022_MDRPart1_PaymentsInitiation_2023_2024_v2.pdf
URL: s3://mojsuperbucket/general/ISO20022_MDRPart1_PaymentsInitiation_2023_2024_v2.pdf
Domena: general
Typ: .pdf
Tagi: s3, general
ID Fragmentu: 8c52248350cddf8abe5fec880e214aab
Treść:
| :--- | :--- |
| 1.0 | December 2023 |

The ISO 20022 MessageDefinitions were contributed by SWIFT.

--- DOKUMENT 2 (Relewancja: 0.5888) ---
Tytuł: swift_iso_20022_for_dummies_6th_edition_dec_2022.pdf
Źródło (plik): s3://mojsuperbucket/general/swift_iso_20022_for_dummies_6th_edition_dec_2022.pdf
URL: s3://mojsuperbucket/general/swift_iso_20022_for_dummies_6th_edition_dec_2022.pdf
Domena: general
Typ: .pdf
Tagi: s3, general
ID Fragmentu: 1be5747825e029b0e2e12d2c1a8a9b21
Treść:
CHAPTER 2: Putting ISO 20022 into Practice
Understanding How Standards Coexist
"""


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


def test_normalize_sources_parses_native_document_dump() -> None:
    """structured_content {"result": <dump>} → one SourceRef per DOKUMENT block."""
    sources = _normalize_sources({"result": DUMP}, "rag_iso20022")
    assert len(sources) == 2
    first = sources[0]
    assert first["source_type"] == "rag_iso20022"
    assert first["source_id"] == "8c52248350cddf8abe5fec880e214aab"
    assert first["title"] == "ISO20022_MDRPart1_PaymentsInitiation_2023_2024_v2.pdf"
    assert first["url"] == (
        "s3://mojsuperbucket/general/ISO20022_MDRPart1_PaymentsInitiation_2023_2024_v2.pdf"
    )
    assert first["section"] == "general"
    assert first["score"] == 0.5943
    assert first["quote"].startswith("| :--- |")
    assert "SWIFT" in first["quote"]
    # Document boundaries are respected: no leakage across DOKUMENT blocks.
    second = sources[1]
    assert "CHAPTER 2" in second["quote"]
    assert "CHAPTER 2" not in first["quote"]
    assert second["score"] == 0.5888
    assert second["source_id"] == "1be5747825e029b0e2e12d2c1a8a9b21"


def test_normalize_sources_bare_dump_string_payload() -> None:
    """A bare dump string (text-block fallback path) normalises the same way."""
    sources = _normalize_sources(DUMP, "rag")
    assert [source["source_id"] for source in sources] == [
        "8c52248350cddf8abe5fec880e214aab",
        "1be5747825e029b0e2e12d2c1a8a9b21",
    ]


def test_normalize_sources_plain_result_string_stays_single_source() -> None:
    """A non-dump "result" string is a plain-text quote, not a parse failure."""
    sources = _normalize_sources({"result": "zwykły tekst bez nagłówków"}, "rag")
    assert len(sources) == 1
    assert sources[0]["quote"] == "zwykły tekst bez nagłówków"


def test_normalize_sources_dump_variants() -> None:
    """Missing Relewancja, empty Treść and same-line content are handled."""
    dump = (
        "--- DOKUMENT 1 ---\n"
        "Tytuł: plik.pdf\n"
        "ID Fragmentu: abc123\n"
        "Treść: treść od razu po nazwie pola\n"
        "\n"
        "--- DOKUMENT 2 ---\n"
        "Tytuł: pusty.pdf\n"
        "ID Fragmentu: def456\n"
        "Treść:\n"
    )
    sources = _normalize_sources({"result": dump}, "rag")
    assert len(sources) == 1  # the document with empty Treść has no quote → dropped
    assert sources[0]["source_id"] == "abc123"
    assert sources[0]["score"] == 0.0
    assert sources[0]["quote"] == "treść od razu po nazwie pola"


async def test_rag_search_normalizes_document_dump(settings: Settings) -> None:
    """End-to-end through the gateway: the real server shape yields sources."""
    tools = FakeToolGateway(sessions={"rag_default": {"result": DUMP}})
    state: WorkflowState = {"query": "co to ISO 20022?"}
    sources = await rag_search(state, tools, settings)
    assert len(sources) == 2
    assert all(source["quote"].strip() for source in sources)


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
    # The RAG tool validates strictly (additionalProperties: false) — the call
    # carries only what the knowledge-base tool accepts: query + top_k, no
    # standards/user/permissions block (default MCPServerConfig.top_k = 5).
    assert rag_args == {"query": "oryginalne pytanie", "top_k": 5}
    assert jira_args["max_results"] == 5
    assert confluence_args["max_results"] == 5
    for args in (jira_args, confluence_args):
        assert args["query"] == "oryginalne pytanie"
        assert args["standards"] == ["ISO20022"]
        assert args["user_id"] == "tester"
        assert args["permissions"] == ["read_iso"]


async def test_rag_search_passes_configured_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_DEFAULT_MCP_COLLECTION", "iso20022_business")
    s = Settings(_env_file=None)
    tools = FakeToolGateway(sessions={"rag_default": []})
    state: WorkflowState = {"query": "co to ISO 20022"}
    await rag_search(state, tools, s)
    assert tools.calls[0]["arguments"] == {
        "query": "co to ISO 20022",
        "top_k": 5,
        "collection_name": "iso20022_business",
    }


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
