"""refresh_loop: the worker-side periodic MCP re-check (pure logic, thread-driven).

The loop parks on the stop event between ticks and runs one
``tools.refresh()`` through the injected ``run_coro`` — the worker binds
that to its process event loop, the tests bind it to ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from iso_insight.pipeline.availability import refresh_loop
from tests.fakes.tools import FakeToolGateway


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _run_failing(coro: Any) -> Any:
    coro.close()
    raise RuntimeError("event loop unavailable")


def test_refresh_loop_ticks_until_stopped() -> None:
    tools = FakeToolGateway(sessions={"rag_default": []})
    stop = threading.Event()
    thread = threading.Thread(target=refresh_loop, args=(tools, 0.5, _run, stop))
    thread.start()
    time.sleep(1.2)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert tools.refresh_calls, "at least one tick should have run"
    assert all(timeout is None for timeout in tools.refresh_calls)


def test_refresh_loop_is_disabled_with_non_positive_interval() -> None:
    tools = FakeToolGateway()
    started = time.monotonic()
    refresh_loop(tools, 0, _run, threading.Event())
    assert time.monotonic() - started < 1.0
    assert tools.refresh_calls == []


def test_refresh_loop_is_a_noop_when_stop_is_already_set() -> None:
    tools = FakeToolGateway()
    stop = threading.Event()
    stop.set()
    started = time.monotonic()
    refresh_loop(tools, 60, _run, stop)
    assert time.monotonic() - started < 1.0
    assert tools.refresh_calls == []


def test_refresh_loop_survives_gateway_errors_and_keeps_ticking() -> None:
    stop = threading.Event()
    thread = threading.Thread(
        target=refresh_loop, args=(FakeToolGateway(), 0.5, _run_failing, stop)
    )
    thread.start()
    time.sleep(1.2)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
