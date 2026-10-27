"""Celery task-queue adapter (CLAUDE.md: the broker client is an adapter).

Implements the :class:`~iso_insight.ports.queue.TaskQueue` port. Tasks
are published *by name* (``send_task``) so the API process never imports
the worker module — the worker registers the task under the same name
(:mod:`iso_insight.workers.tasks`). Broker/backend failures surface as
the domain's :class:`TransientQueueError`; "no worker answers" as
:class:`QueueError`.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import Celery
from celery.result import AsyncResult

from iso_insight.config import Settings
from iso_insight.exceptions import QueueError, TransientQueueError
from iso_insight.ports.queue import TaskQueue
from iso_insight.schemas.mcp import ToolStatus
from iso_insight.schemas.queue import QueueHealth, TaskState

logger = logging.getLogger("iso_insight.queue")

# Kept in sync with the worker's own fallback message (the layers may not import each other).
GENERIC_ERROR = "Analiza nie została zakończona. Spróbuj ponownie."


def build_celery_app(settings: Settings) -> Celery:
    """The Celery application for this process, configured from settings.

    There is deliberately no ``autoretry_for``: the worker task is
    idempotent-safe, so a broker-side retry would double the LLM cost
    while the task already degrades gracefully on transient failures.
    """
    app = Celery("iso_insight")
    app.conf.update(
        broker_url=settings.celery_broker_url,
        result_backend=settings.celery_result_backend,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_track_started=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_result_expires=settings.celery_task_result_expires,
        task_soft_time_limit=settings.celery_task_soft_time_limit,
        task_time_limit=settings.celery_task_time_limit,
        broker_connection_retry_on_startup=True,
    )
    return app


class CeleryTaskQueue(TaskQueue):
    """TaskQueue port backed by a Celery broker + result backend."""

    def __init__(self, app: Celery) -> None:
        self._app = app

    def submit(self, task_name: str, *, kwargs: dict[str, Any]) -> str:
        """Publish a task by name and return its task id (202-style submit)."""
        try:
            result = self._app.send_task(task_name, kwargs=kwargs)
        except Exception as exc:
            raise TransientQueueError(f"Broker niedostępny: {exc.__class__.__name__}") from exc
        return str(result.id)

    def task_state(self, task_id: str) -> TaskState:
        """Poll one task: PENDING/STARTED/... plus the parsed result or error text."""
        try:
            async_result = AsyncResult(task_id, app=self._app)
            state = async_result.state
        except Exception as exc:
            raise TransientQueueError(
                f"Backend wyników niedostępny: {exc.__class__.__name__}"
            ) from exc
        if state == "SUCCESS":
            raw = async_result.result
            return TaskState(state=state, result=raw if isinstance(raw, dict) else None)
        if state == "FAILURE":
            error = str(async_result.result) if async_result.result else GENERIC_ERROR
            return TaskState(state=state, error=error)
        return TaskState(state=state)

    def health(self) -> QueueHealth:
        """Broker reachability + how many workers currently answer ping."""
        try:
            with self._app.connection_for_read() as conn:
                conn.ensure_connection(max_retries=1, timeout=2)
            broker = "up"
        except Exception:
            broker = "down"
        workers = 0
        try:
            replies = self._app.control.inspect(timeout=2).ping()
            workers = len(replies or {})
        except Exception:
            workers = 0
        return QueueHealth(broker=broker, workers=workers)

    def worker_tool_status(self) -> dict[str, ToolStatus]:
        """Broadcast ``mcp_status`` to all workers and aggregate their tool status."""
        try:
            replies = (
                self._app.control.broadcast("mcp_status", arguments={}, reply=True, timeout=3) or []
            )
        except Exception as exc:
            raise QueueError(f"Worker niedostępny: {exc.__class__.__name__}") from exc
        statuses: dict[str, ToolStatus] = {}
        for reply in replies:
            if not isinstance(reply, dict):
                continue
            for worker_addr, payload in reply.items():
                if not isinstance(payload, dict):
                    continue
                statuses[worker_addr] = ToolStatus(
                    connected=list(payload.get("connected", [])),
                    startup_errors=list(payload.get("startup_errors", [])),
                )
        if not statuses:
            raise QueueError("Brak aktywnego workera.")
        return statuses
