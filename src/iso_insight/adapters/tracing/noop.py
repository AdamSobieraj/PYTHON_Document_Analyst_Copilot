"""Disabled tracing: the identity implementation of the :class:`Tracing` port.

Used when no Langfuse keys are configured — every operation becomes a
no-op and :meth:`observe` returns its function untouched, so the
workflow runs with zero tracing overhead and no SDK import required.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from iso_insight.ports.tracing import Tracing


class NoOpTracing(Tracing):
    """Tracing port implementation that traces nothing."""

    @property
    def enabled(self) -> bool:
        return False

    def observe(self, *, name: str, as_type: str = "span") -> Callable[..., Any]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            return func

        return decorator

    @contextmanager
    def trace_attributes(self, **attributes: Any) -> Iterator[None]:
        del attributes
        yield

    def set_generation_details(
        self,
        *,
        model: str,
        model_parameters: dict[str, Any],
        usage_details: dict[str, int],
    ) -> None:
        del model, model_parameters, usage_details

    def flush(self) -> None:
        pass
