"""Optional Langfuse tracing for the agent pipeline.

Tracing is fully opt-in: it activates only when LANGFUSE_PUBLIC_KEY and
LANGFUSE_SECRET_KEY are set (server host via LANGFUSE_HOST). When it is
disabled — or the langfuse package is missing — every helper in this
module is a no-op, so the app behaves exactly as without tracing.

Two hard rules:
- tracing must never raise into the request path (all calls are
  exception-swallowed, failures logged at debug level),
- tracing must never add meaningful overhead when disabled.

What is traced (see the decorated functions):
- each query run (Celery task) as one root observation
  ("agent.run_query") with user_id, session_id (thread_id) and tags,
- every agent node as a span (agent.<node>) with the state in/out,
- every LLM call as a generation (llm.chat) with model, temperature and
  token usage.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger("iso_multiagent.tracing")

try:
    from langfuse import Langfuse  # type: ignore
    from langfuse import observe as _langfuse_observe  # type: ignore

    # v4 API for trace-level attributes (user_id/session_id/tags); v3 used
    # Langfuse.update_current_trace() instead.
    try:
        from langfuse import propagate_attributes as _propagate_attributes
    except Exception:
        _propagate_attributes = None

    _IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:  # langfuse not installed -> tracing disabled
    Langfuse = None  # type: ignore
    _langfuse_observe = None  # type: ignore
    _propagate_attributes = None  # type: ignore
    _IMPORT_ERROR = exc


_client: Optional[Any] = None


def _keys_configured() -> bool:
    """True when both Langfuse API keys are set in the environment."""
    return bool(
        os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
    )


def get_langfuse() -> Optional[Any]:
    """Shared Langfuse client (created lazily), or None when tracing is off.

    The client reads LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY /
    LANGFUSE_HOST from the environment; construction is local and cheap
    (no network validation), events are flushed asynchronously in the
    background by the SDK.
    """
    global _client
    if _client is None:
        if Langfuse is None or not _keys_configured():
            return None
        try:
            _client = Langfuse()
        except Exception as exc:
            logger.debug("Langfuse client init failed: %s", exc)
            return None
    return _client


def tracing_enabled() -> bool:
    """True when tracing is active (langfuse installed and keys configured).

    Safe to call any time — it only performs the (cached, local) client
    creation, no network I/O.
    """
    return get_langfuse() is not None


def _noop_decorator(func: Callable) -> Callable:
    """Identity decorator used in place of Langfuse's when tracing is off."""
    return func


def observe(*args: Any, **kwargs: Any) -> Any:
    """@observe decorator — real Langfuse one when enabled, identity when not.

    Supports both forms: @observe and @observe(name=..., as_type=...).
    """
    if _langfuse_observe is not None and _keys_configured():
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return _langfuse_observe(args[0])
        return _langfuse_observe(*args, **kwargs)

    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]
    return _noop_decorator


@contextmanager
def trace_attributes(**attributes: Any) -> Iterator[None]:
    """Context manager attaching user_id / session_id / tags / metadata to the trace.

    Wrap the code that should run under those trace attributes, e.g.::

        with trace_attributes(user_id=uid, session_id=thread_id, tags=["iso"]):
            ...

    Uses the v4 ``propagate_attributes`` API when available, falling back
    to the v3 ``update_current_trace`` call. No-op when tracing is
    disabled; never raises.
    """
    allowed = {
        key: value
        for key, value in attributes.items()
        if value is not None
        and key in (
            "user_id", "session_id", "tags", "metadata",
            "version", "trace_name", "environment",
        )
    }

    client = get_langfuse()
    if client is None or not allowed:
        yield
        return

    if _propagate_attributes is not None:
        try:
            with _propagate_attributes(**allowed):
                yield
        except Exception as exc:
            logger.debug("propagate_attributes failed: %s", exc)
        return

    # v3 fallback: one-shot update of the current trace.
    try:
        client.update_current_trace(**allowed)
    except Exception as exc:
        logger.debug("update_current_trace failed: %s", exc)
    yield


def set_generation_details(
    model: str,
    model_parameters: Optional[dict] = None,
    usage_details: Optional[dict] = None,
) -> None:
    """Record model, sampling parameters and token usage on the current generation.

    Falls back to generic observation metadata if the SDK does not expose
    the generation-specific update (keeps this compatible across SDK versions).
    """
    client = get_langfuse()
    if client is None:
        return

    payload: dict = {}
    if model:
        payload["model"] = model
    if model_parameters:
        payload["model_parameters"] = model_parameters
    if usage_details:
        payload["usage_details"] = usage_details

    if not payload:
        return

    try:
        client.update_current_generation(**payload)
    except Exception:
        try:
            client.update_current_observation(
                metadata={k: v for k, v in payload.items() if v}
            )
        except Exception as exc:
            logger.debug("set_generation_details failed: %s", exc)


def flush() -> None:
    """Flush buffered events and shut the client down (call on app shutdown)."""
    global _client
    client = _client
    if client is None:
        return
    _client = None
    try:
        client.flush()
    except Exception as exc:
        logger.debug("Langfuse flush failed: %s", exc)
    try:
        client.shutdown()
    except Exception:
        pass


def import_error() -> Optional[Exception]:
    """The import-time error, if the langfuse package is unavailable (else None)."""
    return _IMPORT_ERROR
