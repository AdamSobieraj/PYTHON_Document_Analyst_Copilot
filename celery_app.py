"""Celery worker for the ISO multi-agent analysis service.

Executes the analysis workflow in a separate worker process, with Redis
as broker and result backend — the only execution path in the system:

- POST /agent/query (iso_multiagent_api.py) enqueues run_query and
  returns 202 + task_id,
- this module executes it and stores the result in the Redis backend,
- GET /agent/tasks/{task_id} reads that result back,
- GET /agent/health queries this worker's MCP sessions (mcp_status).

The worker is a separate process, so it owns its own event loop and its
own MCP sessions (the API process has its own). The loop and the MCP
manager are started lazily on the first task and torn down on worker
shutdown — never in the prefork parent, so forked children never
inherit half-started sessions.

Run with:
    celery -A celery_app.celery_app worker --loglevel=info
    (add --pool=solo on Windows, where prefork is unavailable)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, Optional

from celery import Celery
from celery.signals import worker_process_shutdown, worker_shutdown
from celery.worker.control import inspect_command

from config import (
    CELERY_BROKER_URL,
    CELERY_RESULT_BACKEND,
    CELERY_TASK_RESULT_EXPIRES,
    CELERY_TASK_SOFT_TIME_LIMIT,
    CELERY_TASK_TIME_LIMIT,
)
from graph import workflow_app
from mcp_client import mcp_manager
from tracing import flush as flush_tracing
from tracing import observe as langfuse_observe
from tracing import trace_attributes


logger = logging.getLogger("iso_multiagent.celery")


# Generic failure message shown to the caller — the stored result must
# never contain exception internals (they stay in the worker logs).
GENERIC_ERROR = "Analiza nie została zakończona. Spróbuj ponownie."


celery_app = Celery("iso_insight")
celery_app.conf.update(
    broker_url=CELERY_BROKER_URL,
    result_backend=CELERY_RESULT_BACKEND,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # STARTED state becomes visible -> RUNNING in the status endpoint.
    task_track_started=True,
    # Acknowledge only after the task finishes: a worker crash mid-task
    # requeues the work instead of losing it (read-only analysis,
    # a duplicate recompute is safe).
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_result_expires=CELERY_TASK_RESULT_EXPIRES,
    task_soft_time_limit=CELERY_TASK_SOFT_TIME_LIMIT,
    task_time_limit=CELERY_TASK_TIME_LIMIT,
    # Keep retrying at boot if Redis comes up after the worker does.
    broker_connection_retry_on_startup=True,
    # Intentionally no autoretry_for / max_retries: LLM calls are expensive
    # and every node already degrades gracefully instead of failing.
)


# --- Per-process async lifecycle ------------------------------------------------
#
# One event loop per worker process, created lazily on the first task.
# `loop.run_until_complete` may be called repeatedly on the same loop
# (unlike asyncio.run, which closes it) — required because the MCP
# streamable-HTTP transports keep background tasks on the loop they were
# started in. The loop is never created in the prefork parent: each child
# creates its own after the fork. Tasks run sequentially per process
# (concurrency 1), so the loop is bound to a single thread.


_loop: Optional[asyncio.AbstractEventLoop] = None
_mcp_started = False


def _get_loop() -> asyncio.AbstractEventLoop:
    """Create (once) and return this process' event loop."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def _run_async(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a coroutine on this process' event loop and return its result."""
    return _get_loop().run_until_complete(coro)


def _ensure_ready() -> None:
    """Start the MCP sessions once per process, before the first task."""
    global _mcp_started
    if not _mcp_started:
        _run_async(mcp_manager.start())
        _mcp_started = True


def _teardown(sender: Any = None, **kwargs: Any) -> None:
    """Close MCP sessions and the loop, flush tracing — idempotent.

    Registered on both shutdown signals so it works for the solo pool
    (main process) and for each prefork child. No-op where MCP was never
    started (e.g. the prefork parent).
    """
    global _loop, _mcp_started

    if _mcp_started and _loop is not None and not _loop.is_closed():
        try:
            _loop.run_until_complete(mcp_manager.stop())
        except Exception as exc:
            logger.warning("Błąd zamykania sesji MCP: %s", exc)
    if _loop is not None and not _loop.is_closed():
        _loop.close()
    _mcp_started = False
    _loop = None
    flush_tracing()


worker_shutdown.connect(_teardown)
worker_process_shutdown.connect(_teardown)


# --- Task ------------------------------------------------------------------------


@langfuse_observe(name="agent.run_query")
async def _invoke_graph(
    initial_state: dict,
    config: dict,
    user_id: str,
    thread_id: str,
) -> dict:
    """Run the compiled workflow; one root observation per run (agent.run_query)."""
    with trace_attributes(
        user_id=user_id,
        session_id=thread_id,
        tags=["iso-insight", "agent", "async"],
    ):
        return await workflow_app.ainvoke(initial_state, config)


@celery_app.task(bind=True, name="iso_insight.run_query")
def run_query(
    self,
    query: str,
    user_id: str,
    permissions: list,
    thread_id: str,
) -> dict:
    """Execute the full analysis workflow for one question.

    The sole execution path: the API (POST /agent/query) only enqueues
    this task; the result dict is validated by QueryResult in the API
    layer, so keep these fields JSON-safe and complete. Failures are
    logged with the full traceback but stored in the result backend as a
    generic message (no internals leaked).
    """
    _ensure_ready()

    # The longest graph path (reflector failing 3x plus improvements)
    # takes 26 steps, so the limit must exceed LangGraph's default of 25.
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 50,
    }
    initial_state = {
        "query": query,
        "user_id": user_id,
        "permissions": permissions,
    }

    try:
        result = _run_async(_invoke_graph(initial_state, config, user_id, thread_id))
    except Exception:
        logger.exception("Błąd podczas uruchamiania workflow (task %s)", self.request.id)
        raise RuntimeError(GENERIC_ERROR) from None

    return {
        "final_answer": result.get("final_answer", ""),
        "audit_status": result.get("audit_status", ""),
        "confidence": float(result.get("confidence", 0.0)),
        "sources": list(result.get("sources", [])),
        "audit_log": list(result.get("audit_log", [])),
        "thread_id": thread_id,
    }


# --- Worker-side health reporting -------------------------------------------------


@inspect_command()
def mcp_status(state: Any = None, **kwargs: Any) -> dict:
    """Report this worker's MCP sessions (queried by GET /agent/health).

    Runs in the worker process, where the MCP sessions actually live —
    the API process holds none of its own. Before the first task the
    manager is simply not started yet (empty connected list).
    """
    return mcp_manager.status()
