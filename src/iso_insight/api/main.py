"""Thin FastAPI delivery layer (no business logic — CLAUDE.md).

Endpoints build the context/queue via :mod:`iso_insight.bootstrap` and
either run pure logic (input guardrails) or submit/poll tasks through
the :class:`TaskQueue` port. The worker task is referenced by *name*
(the layers may not import each other).

Run locally with a single uvicorn worker (the checkpointer is
per-process): ``python -m iso_insight.api.main``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from iso_insight.bootstrap import build_context, build_task_queue
from iso_insight.config import get_settings
from iso_insight.exceptions import QueueError, TransientQueueError
from iso_insight.pipeline.guardrails import input_guardrails
from iso_insight.schemas.api import QueryRequest, QueryResult, SubmitResponse, TaskStatusResponse

logger = logging.getLogger("iso_insight.api")

# The worker task is addressed by name — the API process never imports the
# worker module (same layer; the queue adapter publishes it with send_task).
RUN_QUERY_TASK_NAME = "iso_insight.run_query"

# Broker/backend unavailability is a 503 for the caller.
BROKER_DOWN_DETAIL = "Broker niedostępny. Spróbuj ponownie."
# Celery state -> API status (PENDING also means "never started").
_STATUS_MAP: dict[str, str] = {
    "PENDING": "QUEUED",
    "RECEIVED": "QUEUED",
    "STARTED": "RUNNING",
    "RETRY": "RETRYING",
    "SUCCESS": "SUCCESS",
    "FAILURE": "FAILURE",
    "REVOKED": "REVOKED",
}

_settings = get_settings()
_context = build_context()
_queue = build_task_queue(_settings, api_mode=True)

# The guardrails run synchronously in-process; the span is attached here,
# at the boundary, so the domain module stays tracing-free.
_run_guard = _context.tracing.observe(name="agent.input_guard")(input_guardrails)

api = FastAPI(title="ISO Insight API", version="1.0.0")
router = APIRouter(prefix="/agent")


@router.post("/query", response_model=SubmitResponse, status_code=202)
def submit_query(payload: QueryRequest) -> SubmitResponse:
    """Run input guardrails, then queue the analysis (202 Accepted).

    Never logs the query itself (privacy rule) — only the task id.
    """
    guard_state = _run_guard({"query": payload.query})
    if guard_state.get("blocked"):
        raise HTTPException(
            status_code=400,
            detail=str(guard_state.get("final_answer", "Zapytanie zablokowane.")),
        )
    thread_id = payload.thread_id or str(uuid.uuid4())
    try:
        task_id = _queue.submit(
            RUN_QUERY_TASK_NAME,
            kwargs={
                "query": payload.query,
                "user_id": payload.user_id,
                "permissions": payload.permissions,
                "thread_id": thread_id,
            },
        )
    except TransientQueueError:
        logger.exception("Could not submit task for thread %s", thread_id)
        raise HTTPException(status_code=503, detail=BROKER_DOWN_DETAIL) from None
    return SubmitResponse(
        task_id=task_id,
        thread_id=thread_id,
        status="QUEUED",
        status_url=f"/agent/tasks/{task_id}",
    )


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
def get_task(task_id: str) -> TaskStatusResponse:
    """Poll one task; on SUCCESS the stored payload is validated as QueryResult."""
    try:
        state = _queue.task_state(task_id)
    except TransientQueueError:
        raise HTTPException(status_code=503, detail=BROKER_DOWN_DETAIL) from None

    status = _STATUS_MAP.get(state.state, state.state)

    if state.state == "SUCCESS" and state.result is not None:
        try:
            result = QueryResult(**state.result)
        except (ValueError, TypeError):
            logger.exception("Malformed stored result for task %s", task_id)
            raise HTTPException(
                status_code=500,
                detail="Wynik zadania jest nieprawidłowy. Sprawdź logi workera.",
            ) from None
        return TaskStatusResponse(task_id=task_id, status=status, result=result)

    if state.state == "FAILURE":
        return TaskStatusResponse(
            task_id=task_id,
            status=status,
            error=state.error or "Analiza nie została zakończona. Spróbuj ponownie.",
        )

    return TaskStatusResponse(task_id=task_id, status=status)


@router.get("/health")
def agent_health() -> dict[str, Any]:
    """MCP tool status aggregated over all workers (via the queue broadcast)."""
    try:
        statuses = _queue.worker_tool_status()
    except QueueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    connected: list[str] = []
    startup_errors: list[str] = []
    for status in statuses.values():
        for name in status.connected:
            if name not in connected:
                connected.append(name)
        startup_errors.extend(status.startup_errors)
    healthy = bool(connected) and not startup_errors
    return {
        "status": "ok" if healthy else "degraded",
        "workers": len(statuses),
        "connected": connected,
        "startup_errors": startup_errors,
    }


@api.get("/health")
def health() -> JSONResponse:
    """Root health: broker reachable and at least one worker answers ping."""
    queue_health = _queue.health()
    healthy = queue_health.broker == "up" and queue_health.workers > 0
    status_code = 200 if healthy else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if healthy else "degraded",
            "broker": queue_health.broker,
            "workers": queue_health.workers,
        },
    )


api.include_router(router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(api, host="0.0.0.0", port=8000)
