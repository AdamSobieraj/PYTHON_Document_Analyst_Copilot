"""Langfuse tracing adapter (CLAUDE.md: the tracing SDK is an adapter).

Translates the :class:`~iso_insight.ports.tracing.Tracing` port into the
langfuse SDK. Everything that leaves the process is PII-masked through
the pure-logic ``observability.masking`` module first. The SDK is
imported lazily and tolerated when missing: without it (or without keys)
the adapter degrades to a no-op rather than crashing the workflow.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

from iso_insight.config import Settings
from iso_insight.observability.masking import mask_pii
from iso_insight.ports.tracing import Tracing

logger = logging.getLogger("iso_insight.tracing")

try:  # the SDK is optional at import time — a missing package must not break the workflow
    from langfuse import Langfuse
    from langfuse import observe as _langfuse_observe
except Exception:  # pragma: no cover - exercised only when langfuse is absent
    Langfuse = None  # type: ignore[assignment,misc]
    _langfuse_observe = None  # type: ignore[assignment]

try:  # v4+ API; older versions only have update_current_trace
    from langfuse import propagate_attributes
except Exception:  # pragma: no cover - version-dependent
    propagate_attributes = None  # type: ignore[assignment]

# Only these attribute names are forwarded to Langfuse (port contract);
# anything else is dropped before it can leave the process.
_TRACE_KEYS = frozenset(
    {"user_id", "session_id", "tags", "metadata", "version", "trace_name", "environment"}
)


def _mask_value(value: Any) -> Any:
    """Recursively mask PII in strings before they are sent to Langfuse."""
    if isinstance(value, str):
        return mask_pii(value)
    if isinstance(value, dict):
        return {key: _mask_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_value(item) for item in value]
    return value


class LangfuseTracing(Tracing):
    """Tracing port backed by the langfuse SDK, selected by configured keys."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any | None = None

    @property
    def enabled(self) -> bool:
        return self._import_error() is None and self._keys_configured()

    def _import_error(self) -> str | None:
        if _langfuse_observe is None or Langfuse is None:
            return "langfuse nie jest zainstalowany"
        return None

    def _keys_configured(self) -> bool:
        return bool(self._settings.langfuse_public_key and self._settings.langfuse_secret_key)

    def _get_client(self) -> Any | None:
        """Lazily create the SDK client; None when the adapter is not usable."""
        if self._client is None:
            if not self.enabled:
                return None
            kwargs: dict[str, Any] = {
                "public_key": self._settings.langfuse_public_key,
                "secret_key": self._settings.langfuse_secret_key,
            }
            if self._settings.langfuse_host:
                kwargs["host"] = self._settings.langfuse_host
            try:
                self._client = Langfuse(**kwargs)
            except Exception:  # SDK constructor is defensive: tracing must not break the run
                logger.debug("Langfuse client init failed; tracing disabled", exc_info=True)
                return None
        return self._client

    def observe(self, *, name: str, as_type: str = "span") -> Callable[..., Any]:
        """Wrap a function in a Langfuse span/generation, or leave it untouched when off."""
        if not self.enabled:
            return lambda func: func
        decorated = _langfuse_observe(name=name, as_type=as_type)  # type: ignore[call-overload]
        return cast(Callable[..., Any], decorated)

    @contextmanager
    def trace_attributes(self, **attributes: Any) -> Iterator[None]:
        """Attach allow-listed, PII-masked attributes to the current trace."""
        if not self.enabled:
            yield
            return
        allowed = {
            key: _mask_value(value)
            for key, value in attributes.items()
            if key in _TRACE_KEYS and value is not None
        }
        if not allowed:
            yield
            return
        if propagate_attributes is not None:
            try:
                with propagate_attributes(**allowed):
                    yield
            except Exception:
                logger.debug("propagate_attributes failed", exc_info=True)
            return
        client = self._get_client()
        if client is not None:
            try:
                client.update_current_trace(**allowed)
            except Exception:
                logger.debug("update_current_trace failed", exc_info=True)
        yield

    def set_generation_details(
        self,
        *,
        model: str,
        model_parameters: dict[str, Any],
        usage_details: dict[str, int],
    ) -> None:
        """Attach model/usage details to the current generation observation."""
        if not self.enabled:
            return
        client = self._get_client()
        if client is None:
            return
        payload: dict[str, Any] = {"model": model}
        if model_parameters:
            payload["model_parameters"] = model_parameters
        if usage_details:
            payload["usage_details"] = usage_details
        try:
            client.update_current_generation(**payload)
        except Exception:
            try:  # older SDK versions expose only the generic observation update
                client.update_current_observation(metadata=payload)
            except Exception:
                logger.debug("generation details update failed", exc_info=True)

    def flush(self) -> None:
        """Flush and close the SDK client (idempotent)."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.flush()
        except Exception:
            logger.debug("langfuse flush failed", exc_info=True)
        try:
            client.shutdown()
        except Exception:
            pass
