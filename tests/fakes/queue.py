"""In-memory TaskQueue fake with a broker-down toggle.

``broker_down`` makes ``submit``/``task_state`` raise
:class:`TransientQueueError` and ``health`` report the broker down,
mirroring the real adapter with an unreachable Redis.
"""

from __future__ import annotations

from typing import Any

from iso_insight.exceptions import QueueError, TransientQueueError
from iso_insight.ports.queue import TaskQueue
from iso_insight.schemas.mcp import ToolStatus
from iso_insight.schemas.queue import QueueHealth, TaskState


class FakeTaskQueue(TaskQueue):
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []
        self.states: dict[str, TaskState] = {}
        self.worker_statuses: dict[str, ToolStatus] = {}
        self.broker_down = False
        self._counter = 0

    def submit(self, task_name: str, *, kwargs: dict[str, Any]) -> str:
        if self.broker_down:
            raise TransientQueueError("Broker niedostępny: fake")
        self._counter += 1
        task_id = f"fake-task-{self._counter}"
        self.submitted.append({"task_name": task_name, "kwargs": dict(kwargs), "task_id": task_id})
        return task_id

    def task_state(self, task_id: str) -> TaskState:
        if self.broker_down:
            raise TransientQueueError("Backend wyników niedostępny: fake")
        return self.states.get(task_id) or TaskState(state="PENDING")

    def health(self) -> QueueHealth:
        return QueueHealth(
            broker="down" if self.broker_down else "up", workers=len(self.worker_statuses)
        )

    def worker_tool_status(self) -> dict[str, ToolStatus]:
        if not self.worker_statuses:
            raise QueueError("Brak aktywnego workera.")
        return dict(self.worker_statuses)
