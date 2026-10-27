"""Unit tests for the Pydantic data contracts."""

import pytest
from pydantic import ValidationError

from iso_insight.schemas.api import QueryRequest, QueryResult, SourceOut
from iso_insight.schemas.mcp import MCPServerConfig
from iso_insight.schemas.queue import QueueHealth, TaskState


def test_query_request_defaults() -> None:
    req = QueryRequest(query="Czy pole :20: musi być unikalne?")
    assert req.user_id == "anonymous"
    assert req.thread_id is None
    assert "read_iso" in req.permissions
    assert "read_rag" in req.permissions


def test_query_request_requires_min_length() -> None:
    with pytest.raises(ValidationError):
        QueryRequest(query="ab")


def test_query_result_roundtrip() -> None:
    result = QueryResult(
        final_answer="Tak, pole :20: musi być unikalne.",
        audit_status="completed",
        confidence=0.9,
        sources=[SourceOut(source_type="rag", title="ISO 20022")],
        audit_log=[{"event": "input_allowed"}],
        thread_id="t-1",
    )
    restored = QueryResult.model_validate(result.model_dump())
    assert restored == result


def test_mcp_server_config_frozen() -> None:
    cfg = MCPServerConfig(url="http://x/mcp", tool="rag_search")
    with pytest.raises(ValidationError):
        cfg.url = "http://y/mcp"


def test_task_state_and_queue_health_defaults() -> None:
    state = TaskState(state="PENDING")
    assert state.result is None
    assert state.error is None
    health = QueueHealth(broker="ok")
    assert health.workers == 0
