"""Integration: a real worker + real broker roundtrip (needs ``make up``).

Spawns an actual Celery worker against the compose broker and drives one
``run_query`` through the real queue adapter end to end. Skips itself
when the broker is unreachable. Marked ``integration`` — excluded from
the default gate, run with ``pytest -m integration``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from iso_insight.adapters.queue.celery_queue import CeleryTaskQueue, build_celery_app
from iso_insight.config import Settings
from iso_insight.exceptions import TransientQueueError
from iso_insight.schemas.queue import TaskState

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"

ALLOWED_STATUSES = {
    "ok",
    "high_confidence",
    "low_confidence",
    "escalation_required",
    "missing_sources",
    "draft_missing",
}


def _queue(settings: Settings) -> CeleryTaskQueue:
    return CeleryTaskQueue(build_celery_app(settings))


def _wait_for(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(1.0)
    return predicate()


def _require_broker(queue: CeleryTaskQueue) -> None:
    if queue.health().broker != "up":
        pytest.skip("broker (redis) niedostępny — uruchom `make up` i powtórz")


def test_broker_reports_up(settings: Settings) -> None:
    _require_broker(_queue(settings))


def test_real_worker_processes_a_run_query(settings: Settings) -> None:
    queue = _queue(settings)
    _require_broker(queue)

    pythonpath = os.environ.get("PYTHONPATH", "")
    paths = [str(SRC_DIR), pythonpath] if pythonpath else [str(SRC_DIR)]
    worker = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "iso_insight.workers.tasks",
            "worker",
            "--loglevel=warning",
            "--pool=solo",
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(paths)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_for(lambda: queue.health().workers >= 1, timeout=60.0), (
            "worker nie odpisał na ping"
        )
        task_id = queue.submit(
            "iso_insight.run_query",
            kwargs={
                "query": "Jakie są wymagania ISO 20022 dla wiadomości pacs.002?",
                "user_id": "tester",
                "permissions": ["read_iso"],
                "thread_id": "real-worker-test",
            },
        )
        final: TaskState | None = None

        def _terminal() -> bool:
            nonlocal final
            try:
                state = queue.task_state(task_id)
            except TransientQueueError:
                return False
            if state.state in {"SUCCESS", "FAILURE", "REVOKED"}:
                final = state
            return final is not None

        assert _wait_for(_terminal, timeout=180.0), "zadanie nie zakończyło się w limicie"
        assert final is not None
        assert final.state == "SUCCESS", final.error
        payload = final.result
        assert isinstance(payload, dict)
        assert payload.get("audit_status") in ALLOWED_STATUSES
        assert payload.get("final_answer")
    finally:
        worker.terminate()
        worker.wait(timeout=15)
