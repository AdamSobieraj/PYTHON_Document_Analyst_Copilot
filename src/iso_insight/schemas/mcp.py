"""Data contracts for the tool gateway (external MCP servers)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class MCPServerConfig(BaseModel):
    """Connection settings for one MCP server (RAG base, Jira, Confluence).

    Produced by the config layer (``Settings.mcp_servers``), consumed by
    the tool-gateway adapter. Immutable: once built, settings are never
    mutated at runtime.
    """

    model_config = {"frozen": True}

    transport: str = "streamable_http"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    tool: str
    timeout: float = 30.0
    top_k: int = 5


class ToolStatus(BaseModel):
    """Health of the tool gateway as seen by one process (worker)."""

    connected: list[str] = Field(default_factory=list)
    startup_errors: list[str] = Field(default_factory=list)
