"""Worker delivery layer: thin Celery task wrappers around the pipeline.

The worker process owns a per-process event loop (the graph is async, the
Celery task is sync), a lazily built :class:`PipelineContext` and a
compiled workflow. A daemon monitor thread re-checks MCP availability
every ``mcp_availability_check_interval`` seconds, so a worker picks up
upstream services that start later, without a restart. All of it lives
in module globals because the prefork pool gives every child its own
module state — shared memory between processes is never assumed
(CLAUDE.md), everything is rebuilt per process.

Run with:

    celery -A iso_insight.workers.tasks worker --loglevel=info --pool=prefork -c 1
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Coroutine
from typing import Any, cast

from celery.result import AsyncResult
from celery.signals import worker_process_shutdown, worker_shutdown
from celery.worker.control import inspect_command

from iso_insight.bootstrap import build_celery_application, build_context
from iso_insight.config import get_settings
from iso_insight.pipeline.availability import refresh_loop
from iso_insight.pipeline.context import PipelineContext
from iso_insight.pipeline.stages import RECURSION_LIMIT, build_workflow
from iso_insight.schemas.workflow import WorkflowState
from iso_insight.workers.mcp_status import (
    collect_status,
    file_pids,
    prune_stale,
    publish_status,
    remove_status,
)

logger = logging.getLogger("iso_insight.worker")

# Kept in sync with the queue adapter's copy (the layers may not import each other).
GENERIC_ERROR = "Analiza nie została zakończona. Spróbuj ponownie."

celery_app = build_celery_application(get_settings())
# Alias so ``celery -A iso_insight.workers.tasks`` resolves the app
# (the CLI looks for ``app``/``celery`` attributes on the module).
app = celery_app

# Per-process state (prefork pool: each child process has its own copy).
_loop: asyncio.AbstractEventLoop | None = None
# Serializes event-loop usage: task coroutines and the monitor ticks share one loop.
_loop_lock = threading.Lock()
_ctx: PipelineContext | None = None
_graph: Any = None
_mcp_started = False
# The MCP availability monitor (daemon thread), when running.
_monitor_thread: threading.Thread | None = None
_monitor_stop: threading.Event | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """One long-lived event loop per worker process (created lazily)."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def _run_async(coro: Any) -> Any:
    """Run one coroutine on the process loop (tasks and the monitor share it)."""
    with _loop_lock:
        return _get_loop().run_until_complete(coro)


def _start_mcp_monitor(ctx: PipelineContext) -> None:
    """Start the per-process MCP availability monitor (once, after tools.start)."""
    global _monitor_thread, _monitor_stop
    if _monitor_thread is not None or ctx.settings.mcp_availability_check_interval <= 0:
        return
    _monitor_stop = threading.Event()
    _monitor_thread = threading.Thread(
        target=_run_mcp_monitor,
        args=(ctx, _monitor_stop),
        name="iso-insight-mcp-monitor",
        daemon=True,
    )
    _monitor_thread.start()


def _publish_tool_status() -> None:
    """Publish this process's MCP status to its per-PID file.

    The ``mcp_status`` inspect command runs in the worker's main process,
    which cannot see the child's module globals — the file is the bridge.
    """
    if _ctx is None:
        return
    status = _ctx.tools.status()
    publish_status(os.getpid(), status.connected, status.startup_errors)


def _run_mcp_monitor(ctx: PipelineContext, stop: threading.Event) -> None:
    def tick(coro: Coroutine[Any, Any, Any]) -> Any:
        result = _run_async(coro)
        _publish_tool_status()
        return result

    try:
        refresh_loop(ctx.tools, ctx.settings.mcp_availability_check_interval, tick, stop)
    except Exception:
        logger.debug("MCP monitor loop ended with an error", exc_info=True)


def _stop_mcp_monitor() -> None:
    """Signal the monitor to stop and join it before the loop closes."""
    global _monitor_thread, _monitor_stop
    thread = _monitor_thread
    stop = _monitor_stop
    _monitor_thread = None
    _monitor_stop = None
    if thread is None:
        return
    if stop is not None:
        stop.set()
    thread.join(timeout=60)


def _ensure_ready() -> PipelineContext:
    """Lazily build context + MCP sessions + compiled graph (once per process)."""
    global _ctx, _graph, _mcp_started
    ctx = _ctx
    if ctx is None:
        ctx = build_context()
        _ctx = ctx
    if not _mcp_started:
        _run_async(ctx.tools.start())
        _publish_tool_status()
        _start_mcp_monitor(ctx)
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


def _live_child_pids() -> list[int]:
    """PIDs of this process's live prefork children (empty on any failure).

    The pool object lives on the worker's consumer, not on the app, but
    ``billiard`` — celery's process-pool implementation — records every
    child started in this process in its own registry, and
    ``active_children()`` drops the dead ones before returning.
    """
    try:
        from billiard import active_children

        return [int(proc.pid) for proc in active_children()]
    except Exception:
        logger.debug("billiard.active_children() introspection failed", exc_info=True)
        return []


@inspect_command()  # type: ignore[untyped-decorator]
def mcp_status(state: Any) -> dict[str, Any]:
    """Plain-dict MCP tool status, aggregated by the queue adapter on /agent/health.

    Runs in the worker's *main* process (Celery passes the pidbox ``state``
    as the first argument — unused here), but under the prefork pool the
    MCP sessions live in the children — so the answer is read from the
    children's per-PID status files, limited to their live PIDs. Pools that
    run tasks in the same process (solo, threads) keep ``_ctx`` here and
    answer directly.
    """
    if _ctx is not None:
        status = _ctx.tools.status()
        return {
            "connected": list(status.connected),
            "startup_errors": list(status.startup_errors),
        }
    pids = _live_child_pids() or file_pids()
    prune_stale(pids)
    return collect_status(pids)


def _teardown(**kwargs: Any) -> None:
    """Shut down the monitor, MCP sessions, tracing and the loop (once per process)."""
    global _loop, _ctx, _graph, _mcp_started
    _stop_mcp_monitor()
    remove_status(os.getpid())
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
