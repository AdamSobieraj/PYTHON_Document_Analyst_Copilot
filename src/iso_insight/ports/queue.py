"""Port: the async task queue (Celery/Redis is one adapter behind this).

The API process submits analysis runs and polls their state through
this interface. Which queue implementation is used is a composition-
root decision (config), never a domain one.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from iso_insight.schemas.mcp import ToolStatus
from iso_insight.schemas.queue import QueueHealth, TaskState


class TaskQueue(ABC):
    """Submit tasks, read their state, report fleet health.

    Raises :class:`~iso_insight.exceptions.TransientQueueError` when the
    broker / result backend is unreachable, :class:`~iso_insight.exceptions.
    QueueError` for permanent failures (e.g. no worker answers the
    health broadcast).
    """

    @abstractmethod
    def submit(self, task_name: str, *, kwargs: dict[str, Any]) -> str:
        """Enqueue a task and return its id."""

    @abstractmethod
    def task_state(self, task_id: str) -> TaskState:
        """Current state of one task (result attached on SUCCESS)."""

    @abstractmethod
    def health(self) -> QueueHealth:
        """Broker liveness plus the number of reachable workers."""

    @abstractmethod
    def worker_tool_status(self) -> dict[str, ToolStatus]:
        """MCP connectivity per worker, via a broadcast to the fleet."""
