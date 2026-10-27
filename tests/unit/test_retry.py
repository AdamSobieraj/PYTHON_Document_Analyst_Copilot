"""Retry policy: transient failures get exponential backoff + jitter, others propagate."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from iso_insight.exceptions import TransientError
from iso_insight.pipeline.retry import retry_transient

Sleep = Callable[[float], Awaitable[None]]


def _recording_sleep(sleeps: list[float]) -> Sleep:
    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    return _sleep


async def test_success_on_first_try_never_sleeps() -> None:
    sleeps: list[float] = []
    calls = 0

    async def ok() -> int:
        nonlocal calls
        calls += 1
        return 42

    result = await retry_transient(ok, sleep=_recording_sleep(sleeps))
    assert result == 42
    assert calls == 1
    assert sleeps == []


async def test_transient_failures_are_retried() -> None:
    sleeps: list[float] = []
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TransientError("chwilowy błąd")
        return "ok"

    result = await retry_transient(flaky, sleep=_recording_sleep(sleeps))
    assert result == "ok"
    assert calls == 3
    assert len(sleeps) == 2
    assert all(delay >= 0.0 for delay in sleeps)


async def test_exhausted_attempts_raise_the_transient_error() -> None:
    sleeps: list[float] = []
    calls = 0

    async def always_fails() -> None:
        nonlocal calls
        calls += 1
        raise TransientError("zawsze pada")

    with pytest.raises(TransientError):
        await retry_transient(always_fails, max_attempts=3, sleep=_recording_sleep(sleeps))
    assert calls == 3
    assert len(sleeps) == 2  # no sleep after the final attempt


async def test_non_transient_error_propagates_immediately() -> None:
    sleeps: list[float] = []
    calls = 0

    async def permanent() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("trwały błąd")

    with pytest.raises(ValueError):
        await retry_transient(permanent, sleep=_recording_sleep(sleeps))
    assert calls == 1
    assert sleeps == []


async def test_delay_is_capped_with_full_jitter() -> None:
    sleeps: list[float] = []

    async def always_fails() -> None:
        raise TransientError("pada")

    with pytest.raises(TransientError):
        await retry_transient(
            always_fails,
            max_attempts=4,
            base_delay=1.0,
            max_delay=4.0,
            sleep=_recording_sleep(sleeps),
        )
    assert len(sleeps) == 3
    caps = (1.0, 2.0, 4.0)  # min(max_delay, base * 2**attempt)
    for delay, cap in zip(sleeps, caps, strict=True):
        assert 0.0 <= delay <= cap
