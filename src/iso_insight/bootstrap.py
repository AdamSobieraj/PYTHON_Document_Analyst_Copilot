"""Composition root: settings → concrete adapters → context.

The ONLY place where concrete adapter implementations are chosen.
Domain code (pipeline/agents/retrieval) receives ports via
:class:`~iso_insight.pipeline.context.PipelineContext` or via the
task queue built here — it never names an adapter class.
"""

from __future__ import annotations

from celery import Celery

from iso_insight.adapters.llm.openai_compatible import OpenAICompatibleLLM
from iso_insight.adapters.queue.celery_queue import (
    CeleryTaskQueue,
)
from iso_insight.adapters.queue.celery_queue import (
    build_celery_app as _build_celery_app,
)
from iso_insight.adapters.tools.mcp import MCPToolGateway
from iso_insight.adapters.tracing.langfuse import LangfuseTracing
from iso_insight.adapters.tracing.noop import NoOpTracing
from iso_insight.config import Settings, get_settings
from iso_insight.pipeline.context import PipelineContext
from iso_insight.ports.llm import LLMClient
from iso_insight.ports.queue import TaskQueue
from iso_insight.ports.tools import ToolGateway
from iso_insight.ports.tracing import Tracing


def build_tracing(settings: Settings) -> Tracing:
    """Langfuse when configured (and importable), otherwise the no-op adapter.

    Tracing must never take the workflow down, so the decision is made
    here at composition time from the adapter's own ``enabled`` flag
    (import + key check) rather than by exception.
    """
    langfuse = LangfuseTracing(settings)
    if langfuse.enabled:
        return langfuse
    return NoOpTracing()


def build_llm(settings: Settings, tracing: Tracing) -> LLMClient:
    return OpenAICompatibleLLM(settings, tracing)


def build_tools(settings: Settings) -> ToolGateway:
    return MCPToolGateway(settings)


def build_context(settings: Settings | None = None) -> PipelineContext:
    """Assemble every port the pipeline needs, from (cached) settings."""
    settings = settings or get_settings()
    tracing = build_tracing(settings)
    return PipelineContext(
        settings=settings,
        llm=build_llm(settings, tracing),
        tools=build_tools(settings),
        tracing=tracing,
    )


def build_celery_application(settings: Settings | None = None) -> Celery:
    """The raw Celery app (the workers delivery layer registers tasks on it)."""
    return _build_celery_app(settings or get_settings())


def build_task_queue(settings: Settings | None = None, *, api_mode: bool = False) -> TaskQueue:
    """The Celery-backed task queue.

    In ``api_mode`` the result-backend transport retries are disabled so a
    missing Redis fails fast (503) instead of blocking the request until
    the broker connection timeout.
    """
    settings = settings or get_settings()
    app = build_celery_application(settings)
    if api_mode:
        app.conf.result_backend_transport_options = {"retry_policy": {"max_retries": 0}}
    return CeleryTaskQueue(app)
