"""Integration: one real Celery task roundtrip in eager mode (no broker).

The graph runs on the fake LLM/tools; only the delivery layer is real —
task dispatch, the idempotency check against a down result backend and
the result envelope. Marked ``integration`` — excluded from the default
gate, run with ``pytest -m integration``.
"""

from __future__ import annotations

from typing import Any

import pytest

from iso_insight.adapters.tracing.noop import NoOpTracing
from iso_insight.config import Settings
from iso_insight.pipeline.context import PipelineContext
from iso_insight.pipeline.stages import build_workflow
from iso_insight.workers import tasks
from tests.fakes.llm import FakeLLM
from tests.fakes.tools import FakeToolGateway

pytestmark = pytest.mark.integration


def test_run_query_task_completes_in_eager_mode(
    settings: Settings, fake_llm: FakeLLM, fake_tools: FakeToolGateway
) -> None:
    context = PipelineContext(
        settings=settings, llm=fake_llm, tools=fake_tools, tracing=NoOpTracing()
    )
    tasks._ctx = context
    tasks._graph = build_workflow(context)
    # The fake gateway needs no MCP handshake — skip it in the worker task.
    tasks._mcp_started = True
    tasks.celery_app.conf.update(task_always_eager=True, task_eager_propagates=True)
    try:
        result = tasks.run_query.apply(
            kwargs={
                "query": "Jakie są wymagania ISO 20022 dla wiadomości pacs.002?",
                "user_id": "tester",
                "permissions": ["read_iso"],
                "thread_id": "eager-test",
            }
        )
    finally:
        tasks._teardown()
        tasks.celery_app.conf.update(task_always_eager=False, task_eager_propagates=False)

    assert result.successful()
    payload: dict[str, Any] = result.result
    assert payload["audit_status"] == "ok"
    assert payload["final_answer"]
    assert payload["thread_id"] == "eager-test"
