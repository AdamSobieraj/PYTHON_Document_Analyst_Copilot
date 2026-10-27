"""Task-queue data contracts."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class TaskState(BaseModel):
    """One task's state as seen by the caller."""

    state: str  # PENDING | STARTED | RETRY | SUCCESS | FAILURE | REVOKED
    result: dict[str, Any] | None = None
    error: str | None = None


class QueueHealth(BaseModel):
    """Broker liveness plus worker reachability."""

    broker: str  # "up" | "down"
    workers: int = 0
