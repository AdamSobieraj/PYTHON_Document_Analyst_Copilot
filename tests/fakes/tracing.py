"""In-memory Tracing fake: records spans, trace attributes, generation details.

``observe`` wraps both sync and async functions (the wrapper returns
whatever the wrapped function returns — a value or a coroutine).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from iso_insight.ports.tracing import Tracing


class FakeTracing(Tracing):
    def __init__(self) -> None:
        self.spans: list[str] = []
        self.span_types: list[str] = []
        self.attributes: list[dict[str, Any]] = []
        self.generation_details: list[dict[str, Any]] = []
        self.flush_count = 0

    @property
    def enabled(self) -> bool:
        return True

    def observe(self, *, name: str, as_type: str = "span") -> Callable[..., Any]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                self.spans.append(name)
                self.span_types.append(as_type)
                return func(*args, **kwargs)

            return wrapper

        return decorator

    @contextmanager
    def trace_attributes(self, **attributes: Any) -> Iterator[None]:
        self.attributes.append(dict(attributes))
        yield

    def set_generation_details(
        self,
        *,
        model: str,
        model_parameters: dict[str, Any],
        usage_details: dict[str, int],
    ) -> None:
        self.generation_details.append(
            {
                "model": model,
                "model_parameters": dict(model_parameters),
                "usage_details": dict(usage_details),
            }
        )

    def flush(self) -> None:
        self.flush_count += 1
