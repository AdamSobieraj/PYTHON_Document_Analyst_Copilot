"""ISO Insight — multi-agent document analyst copilot.

Hexagonal layout (see CLAUDE.md):

- ``schemas``       — Pydantic v2 data contracts (+ the LangGraph state types),
- ``ports``         — interfaces to external systems (LLM, tracing, tools, queue),
- ``adapters``      — concrete implementations (only place for infra imports),
- ``pipeline``      — workflow stages, LangGraph wiring, pure helpers,
- ``agents``        — agent node logic (LLM only via ports),
- ``retrieval``     — search over RAG / Jira / Confluence (via the tools port),
- ``llm``           — versioned prompt files + structured-output schemas,
- ``observability`` — PII masking and other pure logic,
- ``api`` / ``workers`` — thin delivery layers,
- ``bootstrap``     — the only composition root.
"""

__version__ = "0.1.0"
