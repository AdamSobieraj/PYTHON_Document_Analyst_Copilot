"""Typed workflow state shared by all LangGraph nodes.

WorkflowState is a (fully optional) TypedDict that flows through the graph:
every node reads what it needs, updates its outputs and returns the state.
append_audit() grows the thread-level audit trail that is returned to the
API caller and kept across turns of the same thread.
"""

from __future__ import annotations

from typing import TypedDict, Optional, List, Dict, Any, Literal


class SourceRef(TypedDict, total=False):
    """One piece of retrieved evidence (RAG document, Jira ticket, Confluence page).

    Produced by the search wrappers in tools.py, consumed by the analysis
    nodes and returned in the API response ("sources"). All fields are
    optional (total=False) so partial payloads from different MCP services
    stay valid; "quote" is the only field the pipeline relies on
    (evidence_validator drops sources without one).
    """

    source_type: str
    source_id: str
    title: str
    url: Optional[str]
    section: Optional[str]
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

    detected_standards: List[str]
    selected_rag_servers: List[str]

    user_id: str
    permissions: List[str]

    blocked: bool
    block_reason: str

    iso_requirements: List[str]
    rag_context: List[str]
    sources: List[SourceRef]

    gaps: List[str]
    impact: List[str]

    draft_answer: str
    draft_caveats: List[str]

    critic_verdict: Optional[Literal["pass", "fail"]]
    critic_feedback: str

    confidence: float
    iteration: int

    final_answer: str
    audit_status: str
    audit_log: List[Dict[str, Any]]


def append_audit(
    state: WorkflowState,
    event: str,
    **details: Any,
) -> WorkflowState:
    """Append one entry to the thread's audit log and return the state.

    The entry is {"event": event, **details} — use plain JSON-safe values
    only, because the log is returned to the API caller and stored in
    Celery task results (JSON-serialized). Mutates the state in place and
    returns it so calls can be chained. The log grows across turns of the
    same thread: the orchestrator node deliberately does not reset it.
    """
    state.setdefault("audit_log", [])
    state["audit_log"].append({"event": event, **details})
    return state
