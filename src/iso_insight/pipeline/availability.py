"""Periodic MCP availability re-check (pure logic; the worker drives it).

The worker starts one daemon thread per process that parks on the stop
event between ticks and runs one ``tools.refresh()`` through the injected
``run_coro`` (the worker binds it to its process event loop, tests bind
it to ``asyncio.run``). The loop owns no threads and no event loop of
its own — it is pure logic, so it stays unit-testable.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Coroutine
from typing import Any

from iso_insight.ports.tools import ToolGateway

logger = logging.getLogger("iso_insight.availability")


def refresh_loop(
    tools: ToolGateway,
    interval: float,
    run_coro: Callable[[Coroutine[Any, Any, Any]], Any],
    stop: threading.Event,
) -> None:
    """Tick ``tools.refresh()`` every ``interval`` seconds until ``stop`` is set.

    Never raises: a failed tick is logged and the loop keeps ticking
    (surviving transient failures is the whole point of the monitor).
    ``interval <= 0`` disables the loop.
    """
    if interval <= 0:
        return
    while not stop.wait(timeout=interval):
        try:
            status = run_coro(tools.refresh())
        except Exception:
            logger.warning("MCP availability refresh failed", exc_info=True)
        else:
            logger.info("MCP availability check: %d servers connected", len(status.connected))
