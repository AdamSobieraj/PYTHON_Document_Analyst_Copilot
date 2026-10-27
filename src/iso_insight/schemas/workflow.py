"""The LangGraph workflow state (TypedDicts, as required by StateGraph).

Pydantic is not used here on purpose: ``StateGraph`` requires a
TypedDict (or dataclass) as its state type. API-facing contracts live
in :mod:`iso_insight.schemas.api` instead.

WorkflowState flows through the graph: every node reads what it needs,
updates its outputs and returns the state. The audit log grows across
turns of the same thread (the orchestrator deliberately does not reset
it); the append helper lives in the pipeline (pure logic).
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


class SourceRef(TypedDict, total=False):
    """One piece of retrieved evidence (RAG document, Jira ticket, Confluence page).

    Produced by the retrieval stage, consumed by the analysis nodes and
    returned in the API response ("sources"). All fields are optional
    (total=False) so partial payloads from different MCP services stay
    valid; "quote" is the only field the pipeline relies on
    (the evidence validator drops sources without one).
    """

    source_type: str
    source_id: str
    title: str
    url: str | None
    section: str | None
    quote: str
    score: float


class WorkflowState(TypedDict, total=False):
    """Shared state of one analysis run.

    Field groups:
    - input:      query, original_query, retrieval_query, user_id, permissions
    - routing:    detected_standards, selected_rag_servers
    - guardrails: blocked, block_reason
    - evidence:   iso_requirements, rag_context, sources
    - analysis:   gaps, impact
    - answer:     draft_answer, draft_caveats
    - critic:     critic_verdict, critic_feedback, confidence, iteration
    - output:     final_answer, audit_status, audit_log
    """

    query: str
    original_query: str
    retrieval_query: str

    detected_standards: list[str]
    selected_rag_servers: list[str]

    user_id: str
    permissions: list[str]

    blocked: bool
    block_reason: str

    iso_requirements: list[str]
    rag_context: list[str]
    sources: list[SourceRef]

    gaps: list[str]
    impact: list[str]

    draft_answer: str
    draft_caveats: list[str]

    critic_verdict: Literal["pass", "fail"] | None
    critic_feedback: str

    confidence: float
    iteration: int

    final_answer: str
    audit_status: str
    audit_log: list[dict[str, Any]]
