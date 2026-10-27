"""Worker delivery layer: thin Celery task wrappers around the pipeline.

The worker process owns a per-process event loop (the graph is async, the
Celery task is sync), a lazily built :class:`PipelineContext` and a
compiled workflow. All of it lives in module globals because the prefork
pool gives every child its own module state — shared memory between
processes is never assumed (CLAUDE.md), everything is rebuilt per process.

Run with:

    celery -A iso_insight.workers.tasks worker --loglevel=info --pool=prefork -c 1
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from celery.result import AsyncResult
from celery.signals import worker_process_shutdown, worker_shutdown
from celery.worker.control import inspect_command

from iso_insight.bootstrap import build_celery_application, build_context
from iso_insight.config import get_settings
from iso_insight.pipeline.context import PipelineContext
from iso_insight.pipeline.stages import RECURSION_LIMIT, build_workflow
from iso_insight.schemas.workflow import WorkflowState

logger = logging.getLogger("iso_insight.worker")

# Kept in sync with the queue adapter's copy (the layers may not import each other).
GENERIC_ERROR = "Analiza nie została zakończona. Spróbuj ponownie."

celery_app = build_celery_application(get_settings())
# Alias so ``celery -A iso_insight.workers.tasks`` resolves the app
# (the CLI looks for ``app``/``celery`` attributes on the module).
app = celery_app

# Per-process state (prefork pool: each child process has its own copy).
_loop: asyncio.AbstractEventLoop | None = None
_ctx: PipelineContext | None = None
_graph: Any = None
_mcp_started = False


def _get_loop() -> asyncio.AbstractEventLoop:
    """One long-lived event loop per worker process (created lazily)."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def _run_async(coro: Any) -> Any:
    return _get_loop().run_until_complete(coro)


def _ensure_ready() -> PipelineContext:
    """Lazily build context + MCP sessions + compiled graph (once per process)."""
    global _ctx, _graph, _mcp_started
    ctx = _ctx
    if ctx is None:
        ctx = build_context()
        _ctx = ctx
    if not _mcp_started:
        _run_async(ctx.tools.start())
        _mcp_started = True
    if _graph is None:
        _graph = build_workflow(ctx)
    return ctx


def _invoke_graph(
    ctx: PipelineContext,
    graph: Any,
    initial_state: WorkflowState,
    config: dict[str, Any],
    user_id: str,
    thread_id: str,
) -> WorkflowState:
    """Run the graph with the run-level span + PII-masked trace attributes."""

    async def _run() -> WorkflowState:
        with ctx.tracing.trace_attributes(
            user_id=user_id,
            session_id=thread_id,
            tags=["iso-insight", "agent", "async"],
        ):
            return cast(WorkflowState, await graph.ainvoke(initial_state, config))

    traced = ctx.tracing.observe(name="agent.run_query")(_run)
    return cast(WorkflowState, _run_async(traced()))


@celery_app.task(bind=True, name="iso_insight.run_query")  # type: ignore[untyped-decorator]
def run_query(  # type: ignore[no-untyped-def]
    self,
    query: str,
    user_id: str,
    permissions: list[str],
    thread_id: str,
) -> dict[str, Any]:
    """Run the full analysis for one query and store the result.

    Idempotent: with ``task_acks_late`` + ``task_reject_on_worker_lost`` a
    completed task can be redelivered after a worker crash — the stored
    result is then returned as-is instead of re-running the LLM pipeline.
    (Never logs the query content — privacy rule.)
    """
    try:
        existing = AsyncResult(self.request.id, app=celery_app)
        if existing.state == "SUCCESS" and isinstance(existing.result, dict):
            logger.info("Task %s already completed — returning stored result", self.request.id)
            return existing.result
    except Exception:
        # The result backend may be briefly unavailable; the run must go ahead.
        logger.debug("Idempotency check failed", exc_info=True)

    ctx = _ensure_ready()
    graph = _graph
    assert graph is not None  # _ensure_ready builds it
    config: dict[str, Any] = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": RECURSION_LIMIT,
    }
    initial_state: WorkflowState = {
        "query": query,
        "original_query": query,
        "retrieval_query": query,
        "user_id": user_id,
        "permissions": list(permissions),
    }
    try:
        final_state = _invoke_graph(ctx, graph, initial_state, config, user_id, thread_id)
    except Exception:
        logger.exception("run_query failed for thread %s", thread_id)
        raise RuntimeError(GENERIC_ERROR) from None

    return {
        "final_answer": str(final_state.get("final_answer", "")),
        "audit_status": str(final_state.get("audit_status") or "error"),
        "confidence": float(final_state.get("confidence", 0.0)),
        "sources": [dict(source) for source in final_state.get("sources", [])],
        "audit_log": list(final_state.get("audit_log", [])),
        "thread_id": thread_id,
    }


@inspect_command()  # type: ignore[untyped-decorator]
def mcp_status(self) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Plain-dict MCP tool status, aggregated by the queue adapter on /agent/health."""
    if _ctx is None:
        return {"connected": [], "startup_errors": ["worker nie zainicjalizowany"]}
    status = _ctx.tools.status()
    return {
        "connected": list(status.connected),
        "startup_errors": list(status.startup_errors),
    }


def _teardown(**kwargs: Any) -> None:
    """Shut down MCP sessions, flush tracing and close the loop (once per process)."""
    global _loop, _ctx, _graph, _mcp_started
    ctx = _ctx
    if _mcp_started and ctx is not None:
        try:
            _run_async(ctx.tools.stop())
        except Exception:
            logger.debug("MCP teardown failed", exc_info=True)
    _mcp_started = False
    if ctx is not None:
        try:
            ctx.tracing.flush()
        except Exception:
            logger.debug("Tracing flush failed", exc_info=True)
        _ctx = None
    _graph = None
    if _loop is not None:
        try:
            _loop.close()
        except Exception:
            logger.debug("Event loop close failed", exc_info=True)
        _loop = None


worker_shutdown.connect(_teardown)
worker_process_shutdown.connect(_teardown)
