"""Task queue port contract: fake and Celery adapter share the guarantees.

``health`` never raises; broker/backend outages surface as the domain
``TransientQueueError``; a fleet with no answering worker is a ``QueueError``.
The Celery failure paths skip when a broker is actually reachable — the
integration suite covers the live roundtrip then (CLAUDE.md).
"""

from __future__ import annotations

import pytest

from iso_insight.adapters.queue.celery_queue import CeleryTaskQueue, build_celery_app
from iso_insight.config import Settings
from iso_insight.exceptions import QueueError, TransientQueueError
from iso_insight.ports.queue import TaskQueue
from iso_insight.schemas.mcp import ToolStatus
from iso_insight.schemas.queue import QueueHealth
from tests.fakes.queue import FakeTaskQueue


@pytest.fixture(params=["fake", "celery"])
def queue(request: pytest.FixtureRequest, settings: Settings) -> TaskQueue:
    if request.param == "fake":
        return FakeTaskQueue()
    return CeleryTaskQueue(build_celery_app(settings))


def test_health_never_raises_and_is_well_formed(queue: TaskQueue) -> None:
    health = queue.health()
    assert isinstance(health, QueueHealth)
    assert health.broker in {"up", "down"}
    assert isinstance(health.workers, int)
    assert health.workers >= 0


def test_fake_submit_and_state_roundtrip() -> None:
    queue = FakeTaskQueue()
    task_id = queue.submit("iso_insight.run_query", kwargs={"query": "ISO 20022"})
    assert task_id == "fake-task-1"
    assert queue.task_state(task_id).state == "PENDING"
    assert queue.health() == QueueHealth(broker="up", workers=0)


def test_fake_broker_down_surfaces_transient_errors() -> None:
    queue = FakeTaskQueue()
    queue.broker_down = True
    with pytest.raises(TransientQueueError):
        queue.submit("iso_insight.run_query", kwargs={})
    with pytest.raises(TransientQueueError):
        queue.task_state("fake-task-1")
    assert queue.health().broker == "down"


def test_fake_worker_tool_status_requires_an_answer() -> None:
    queue = FakeTaskQueue()
    with pytest.raises(QueueError):
        queue.worker_tool_status()
    status = ToolStatus(connected=["rag_default"])
    queue.worker_statuses["celery@worker-1"] = status
    assert queue.worker_tool_status() == {"celery@worker-1": status}


def test_celery_submit_reports_transient_when_broker_is_down(settings: Settings) -> None:
    queue = CeleryTaskQueue(build_celery_app(settings))
    if queue.health().broker == "up":
        pytest.skip("broker dostępny — awaria nie jest odtwarzalna w tym środowisku")
    with pytest.raises(TransientQueueError):
        queue.submit("iso_insight.run_query", kwargs={})


def test_celery_task_state_reports_transient_when_backend_is_down(settings: Settings) -> None:
    queue = CeleryTaskQueue(build_celery_app(settings))
    if queue.health().broker == "up":
        pytest.skip("broker dostępny — awaria nie jest odtwarzalna w tym środowisku")
    with pytest.raises(TransientQueueError):
        queue.task_state("brak-takiego-zadania")


def test_celery_worker_tool_status_requires_a_live_worker(settings: Settings) -> None:
    queue = CeleryTaskQueue(build_celery_app(settings))
    if queue.health().workers > 0:
        pytest.skip("worker aktywny — ścieżka pustej floty nie jest odtwarzalna")
    with pytest.raises(QueueError):
        queue.worker_tool_status()
