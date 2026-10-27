"""Transient-failure retry (CLAUDE.md: exponential backoff + jitter).

Pure logic: no adapters, no settings — the caller passes the attempt
limit and the sleep function (tests inject a no-op sleep). Keys off the
domain's :class:`~iso_insight.exceptions.TransientError` and only that;
any other exception propagates immediately.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from iso_insight.exceptions import TransientError

Sleep = Callable[[float], Awaitable[None]]


async def retry_transient[T](
    func: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    sleep: Sleep = asyncio.sleep,
) -> T:
    """Run ``func`` until it succeeds or the attempts are exhausted.

    Only :class:`TransientError` (and subclasses) is retried. The delay
    doubles on each attempt with full jitter:
    ``min(max_delay, base_delay * 2**attempt) * random()``.

    Raises:
        The last :class:`TransientError` when every attempt fails; any
        non-transient exception from the first attempt.
    """
    attempt = 0
    while True:
        try:
            return await func()
        except TransientError:
            if attempt >= max_attempts - 1:
                raise
            delay = min(max_delay, base_delay * (2**attempt)) * random.random()
            attempt += 1
            await sleep(delay)
