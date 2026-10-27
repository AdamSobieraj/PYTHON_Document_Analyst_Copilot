"""Port: tracing / observability.

Langfuse is one adapter behind this interface; when tracing is
configured off, a no-op adapter is used instead. Hard rules: tracing
must never raise into the request path and must stay cheap when
disabled.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any


class Tracing(ABC):
    """Best-effort tracing of the workflow (spans, generations, attributes)."""

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """True when a real backend is configured."""

    @abstractmethod
    def observe(self, *, name: str, as_type: str = "span") -> Callable[..., Any]:
        """Decorator factory for a traced function; the identity decorator when off."""

    @abstractmethod
    def trace_attributes(self, **attributes: Any) -> AbstractContextManager[None]:
        """Context manager: attach allowed attributes to the current trace.

        Only allow-listed keys are forwarded (user_id, session_id, tags,
        metadata, version, trace_name, environment) — never document
        content or PII (masking is pure logic in ``observability``).
        """

    @abstractmethod
    def set_generation_details(
        self,
        *,
        model: str,
        model_parameters: dict[str, Any],
        usage_details: dict[str, int],
    ) -> None:
        """Attach model / parameters / usage to the current generation span."""

    @abstractmethod
    def flush(self) -> None:
        """Flush pending events (called at process shutdown)."""
