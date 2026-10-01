"""FastAPI entry point for the ISO multi-agent analysis service.

The API is a thin submission/status layer over the Celery worker — it
never runs the LangGraph workflow itself and holds no MCP sessions:

- POST /agent/query      — submit an analysis (202 + task_id),
- GET  /agent/tasks/{id} — status and result of a submitted analysis,
- GET  /agent/health     — MCP connectivity as seen by the worker(s),
- GET  /health           — infrastructure health (broker + worker).

The actual analysis runs in the Celery worker (celery_app.run_query).
The API only publishes tasks to the Redis broker, reads task results
back from the result backend, and reports the health of that
infrastructure — so a long analysis can never be lost to an HTTP
timeout: the submission returns immediately and the work survives
independently of any client connection.

Blocked queries (PII, prompt injection) are rejected with a 400 before
they are enqueued — the raw query text must never reach the broker. The
API is the single entry point to the workflow, so this boundary check is
where the guardrails are enforced (there is no guardrail node in the
graph).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from celery_app import celery_app, run_query
from guardrails import input_guardrails


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("iso_multiagent")


# The API process only publishes tasks and reads their state — it should
# fail fast (-> 503) when Redis is down, instead of holding the request in
# Celery's default reconnect loop (~2 minutes). The worker process keeps
# the robust defaults, since it must survive transient Redis blips mid-task.
celery_app.conf.result_backend_transport_options = {
    "retry_policy": {"max_retries": 0},
}


class SourceOut(BaseModel):
    """One evidence item in the response (RAG document, Jira ticket, Confluence page)."""

    source_type: Optional[str] = None
    source_id: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    section: Optional[str] = None
    quote: Optional[str] = None
    score: Optional[float] = None


class QueryRequest(BaseModel):
    """API request: the question, caller identity, permissions, optional thread.

    thread_id is optional — when omitted a new one is generated and
    returned, so multi-turn conversations are opt-in.
    """

    query: str = Field(..., min_length=3)
    user_id: str = "anonymous"
    permissions: List[str] = Field(
        default_factory=lambda: [
            "read_iso",
            "read_rag",
            "read_jira",
            "read_confluence",
        ]
    )
    thread_id: Optional[str] = None


class SubmitResponse(BaseModel):
    """202 response: the analysis was accepted and queued to a worker."""

    task_id: str
    thread_id: str
    status: str
    status_url: str


class QueryResult(BaseModel):
    """The analysis result stored by the worker (returned on SUCCESS).

    The worker's stored payload is validated against this model before it
    reaches the caller, so a malformed result is a 500 with a generic
    message instead of a schema violation on the client side.
    """

    final_answer: str
    audit_status: str
    confidence: float
    sources: List[SourceOut]
    audit_log: List[Dict[str, Any]]
    thread_id: str


class TaskStatusResponse(BaseModel):
    """GET /agent/tasks/{task_id}: task state, result (SUCCESS) or error (FAILURE)."""

    task_id: str
    status: str
    thread_id: Optional[str] = None
    result: Optional[QueryResult] = None
    error: Optional[str] = None


# Celery state -> API status vocabulary (see GET /agent/tasks/{task_id}).
_STATUS_MAP = {
    "PENDING": "QUEUED",
    "RECEIVED": "QUEUED",
    "STARTED": "RUNNING",
    "RETRY": "RETRYING",
    "SUCCESS": "SUCCESS",
    "FAILURE": "FAILURE",
    "REVOKED": "REVOKED",
}

BROKER_DOWN_DETAIL = "Broker niedostępny. Spróbuj ponownie."


api = FastAPI(
    title="ISO Multiagent API",
    version="1.0.0",
)


# Business endpoints of the agent, grouped under the /agent prefix.
router = APIRouter(prefix="/agent")


@router.post("/query", status_code=202, response_model=SubmitResponse)
async def submit_query(payload: QueryRequest) -> SubmitResponse:
    """Queue the full analysis workflow and return immediately with a task id.

    Blocked queries (empty, PII, prompt injection) are rejected with a
    400 *before* anything is enqueued — the raw query text must never
    land in the broker, and a blocked run must not consume a worker slot.
    This boundary check is where the guardrails are enforced: the API is
    the single entry point to the workflow and the graph has no guardrail
    node.

    The run happens in a Celery worker (celery_app.run_query); poll the
    result with GET /agent/tasks/{task_id}. The worker's agent.run_query
    observation is the Langfuse root span for the run. A broker publish
    failure is a 503 (the run was never queued).
    """
    checked = input_guardrails({"query": payload.query})
    if checked.get("blocked"):
        raise HTTPException(
            status_code=400,
            detail=checked.get("final_answer") or "Zapytanie odrzucone.",
        )

    thread_id = payload.thread_id or str(uuid4())

    try:
        async_result = run_query.apply_async(
            kwargs={
                "query": payload.query,
                "user_id": payload.user_id,
                "permissions": payload.permissions,
                "thread_id": thread_id,
            }
        )
    except Exception:
        logger.exception("Błąd publikacji zadania do kolejki")
        raise HTTPException(status_code=503, detail=BROKER_DOWN_DETAIL)

    return SubmitResponse(
        task_id=async_result.id,
        thread_id=thread_id,
        status="QUEUED",
        status_url=f"/agent/tasks/{async_result.id}",
    )


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def task_status(task_id: str) -> TaskStatusResponse:
    """Task state; result on SUCCESS, a sanitized message on FAILURE.

    QUEUED also covers unknown/expired task ids (Celery reports both as
    PENDING); results expire after CELERY_TASK_RESULT_EXPIRES (24 h
    default). The stored exception text is never returned — full
    tracebacks stay in the worker logs.
    """
    async_result = celery_app.AsyncResult(task_id)
    try:
        state = async_result.state  # triggers the Redis read
    except Exception:
        logger.exception("Błąd odczytu stanu zadania %s", task_id)
        raise HTTPException(status_code=503, detail=BROKER_DOWN_DETAIL)

    response = TaskStatusResponse(task_id=task_id, status=_STATUS_MAP.get(state, state))

    if state == "SUCCESS":
        try:
            response.result = QueryResult(**async_result.result)
        except Exception:
            logger.exception("Nieprawidłowy wynik zadania %s", task_id)
            raise HTTPException(
                status_code=500,
                detail="Wynik zadania jest nieprawidłowy. Sprawdź logi workera.",
            )
        response.thread_id = response.result.thread_id
    elif state == "FAILURE":
        response.error = "Analiza nie została zakończona. Spróbuj ponownie."

    return response


@router.get("/health")
async def agent_health() -> Dict[str, Any]:
    """MCP connectivity as seen by the worker(s) running the analyses.

    The API process holds no MCP sessions of its own — the workers do —
    so this queries the worker(s) through Celery's pidbox (the
    mcp_status inspect command registered in celery_app.py). No live
    worker means nothing to report: a 503, not a fake "all connected".
    """
    try:
        responses = (
            celery_app.control.broadcast(
                "mcp_status", arguments={}, reply=True, timeout=3
            )
            or []
        )
        # Each reply is a single-key dict: {worker_id: status}.
        per_worker = {
            worker_id: response
            for reply in responses
            for worker_id, response in reply.items()
        }
    except Exception:
        logger.exception("Błąd odczytu stanu MCP z workera")
        raise HTTPException(
            status_code=503, detail="Worker niedostępny. Sprawdź workera."
        )

    if not per_worker:
        raise HTTPException(
            status_code=503, detail="Brak aktywnego workera. Sprawdź workera."
        )

    connected: List[str] = []
    startup_errors: List[str] = []
    for worker_status in per_worker.values():
        for name in worker_status.get("connected", []):
            if name not in connected:
                connected.append(name)
        startup_errors.extend(worker_status.get("startup_errors", []))

    return {
        "status": "ok" if not startup_errors else "degraded",
        "workers": len(per_worker),
        "connected": connected,
        "startup_errors": startup_errors,
    }


api.include_router(router)


# Infrastructure-level healthcheck (Docker, load balancer) — the service
# is only "healthy" when it can actually accept and process work.
@api.get("/health")
async def health() -> Dict[str, Any]:
    """API liveness plus broker and worker availability.

    Returns 503 when the broker is down (nothing can be submitted) or no
    worker is reachable (nothing can be processed); the payload still
    carries the details for operators.
    """
    broker = "down"
    try:
        connection = celery_app.connection_for_read()
        connection.ensure_connection(max_retries=1, timeout=2)
        broker = "up"
    except Exception:
        logger.debug("Broker niedostępny", exc_info=True)

    try:
        workers = len(celery_app.control.inspect(timeout=2).ping() or {})
    except Exception:
        workers = 0

    payload: Dict[str, Any] = {"broker": broker, "workers": workers}

    if broker != "up" or workers == 0:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", **payload},
        )

    return {"status": "ok", **payload}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(api, host="0.0.0.0", port=8000)
