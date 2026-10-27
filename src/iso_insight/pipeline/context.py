"""The shared context object threaded through the pipeline (CLAUDE.md).

A plain frozen dataclass holding all ports + settings. It is built
once in the composition root (:mod:`iso_insight.bootstrap`) and passed
to the delivery layers and pipeline stages — the domain never
constructs clients itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from iso_insight.config import Settings
from iso_insight.ports.llm import LLMClient
from iso_insight.ports.tools import ToolGateway
from iso_insight.ports.tracing import Tracing


@dataclass(frozen=True)
class PipelineContext:
    """All ports + settings for one process; the single DI carrier."""

    settings: Settings
    llm: LLMClient
    tools: ToolGateway
    tracing: Tracing
