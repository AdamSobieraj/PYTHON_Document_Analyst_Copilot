"""API request/response data contracts (Pydantic v2).

These are the wire format between the client and the API; the worker's
stored result is validated against :class:`QueryResult` before it is
returned to the caller.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SourceOut(BaseModel):
    """One evidence item in the response (RAG document, Jira ticket, Confluence page)."""

    source_type: str | None = None
    source_id: str | None = None
    title: str | None = None
    url: str | None = None
    section: str | None = None
    quote: str | None = None
    score: float | None = None


class QueryRequest(BaseModel):
    """API request: the question, caller identity, permissions, optional thread.

    thread_id is optional — when omitted a new one is generated and
    returned, so multi-turn conversations are opt-in.
    """

    query: str = Field(..., min_length=3)
    user_id: str = "anonymous"
    permissions: list[str] = Field(
        default_factory=lambda: [
            "read_iso",
            "read_rag",
            "read_jira",
            "read_confluence",
        ]
    )
    thread_id: str | None = None


class SubmitResponse(BaseModel):
    """202 response: the analysis was accepted and queued to a worker."""

    task_id: str
    thread_id: str
    status: str
    status_url: str


class QueryResult(BaseModel):
    """The analysis result stored by the worker (returned on SUCCESS).

    The worker's stored payload is validated against this model before
    it reaches the caller, so a malformed result is a 500 with a
    generic message instead of a schema violation on the client side.
    """

    final_answer: str
    audit_status: str
    confidence: float
    sources: list[SourceOut]
    audit_log: list[dict[str, Any]]
    thread_id: str


class TaskStatusResponse(BaseModel):
    """GET /agent/tasks/{task_id}: task state, result (SUCCESS) or error (FAILURE)."""

    task_id: str
    status: str
    thread_id: str | None = None
    result: QueryResult | None = None
    error: str | None = None
