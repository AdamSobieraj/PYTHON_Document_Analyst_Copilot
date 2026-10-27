"""Application settings — environment variables only (pydantic-settings).

Every value comes from the environment (see ``.env.example``); nothing
is hardcoded. Domain code receives a :class:`Settings` instance via the
composition root (``bootstrap.py``) — it never reads env vars itself.

The per-MCP-server groups (``RAG_DEFAULT_MCP_*``, ``JIRA_MCP_*``, ...)
are read by small nested settings classes so each server keeps its own
``<PREFIX>`` env namespace, and are combined into
:attr:`Settings.mcp_servers` — the registry the tool gateway starts
from.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict

from iso_insight.schemas.mcp import MCPServerConfig

# Fallback RAG base used when no ISO-specific knowledge base matches the query.
DEFAULT_RAG_SERVER = "rag_default"

# Maps a detected ISO standard to its dedicated RAG knowledge base.
RAG_STANDARD_MAP: dict[str, str] = {
    "ISO20022": "rag_iso20022",
    "ISO8583": "rag_iso8583",
}


class MCPServerEnv(BaseSettings):
    """One MCP server's settings, read from ``<PREFIX>_<FIELD>`` env vars."""

    model_config = SettingsConfigDict(extra="ignore")

    transport: str = "streamable_http"
    url: str = ""
    token: str = ""
    tool: str = ""
    timeout: float = 30.0
    top_k: int = 5

    def as_entry(self) -> MCPServerConfig:
        """Translate the flat env settings into the shared data contract."""
        headers: dict[str, str] = {}
        token = self.token.strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return MCPServerConfig(
            transport=self.transport,
            url=self.url,
            headers=headers,
            tool=self.tool,
            timeout=self.timeout,
            top_k=self.top_k,
        )


class RagDefaultMCPServer(MCPServerEnv):
    model_config = SettingsConfigDict(env_prefix="RAG_DEFAULT_MCP_", extra="ignore")

    url: str = "http://rag-default:8000/mcp"
    tool: str = "rag_search"


class RagISO20022MCPServer(MCPServerEnv):
    model_config = SettingsConfigDict(env_prefix="RAG_ISO20022_MCP_", extra="ignore")

    url: str = "http://rag-iso20022:8000/mcp"
    tool: str = "rag_search"


class RagISO8583MCPServer(MCPServerEnv):
    model_config = SettingsConfigDict(env_prefix="RAG_ISO8583_MCP_", extra="ignore")

    url: str = "http://rag-iso8583:8000/mcp"
    tool: str = "rag_search"


class JiraMCPServer(MCPServerEnv):
    model_config = SettingsConfigDict(env_prefix="JIRA_MCP_", extra="ignore")

    url: str = "http://jira-mcp-service:8000/mcp"
    tool: str = "jira_search"


class ConfluenceMCPServer(MCPServerEnv):
    model_config = SettingsConfigDict(env_prefix="CONFLUENCE_MCP_", extra="ignore")

    url: str = "http://confluence-mcp-service:8000/mcp"
    tool: str = "confluence_search"


class Settings(BaseSettings):
    """Process-wide settings: workflow tuning, LLM endpoint, task queue."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- workflow tuning ---
    # Max reflector/improve loops before the workflow is forced to output.
    max_iterations: int = 3
    # Answers with lower confidence are escalated to a human analyst.
    low_confidence_threshold: float = 0.6

    # --- LLM (OpenAI-compatible endpoint: {llm_base_url}/chat/completions) ---
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "bank-llm"
    llm_timeout: float = 60.0
    llm_temperature: float = 0.1
    # Empty string means "do not send max_tokens" (model default applies).
    llm_max_tokens: str = ""

    # --- Celery / Redis broker (async queries) ---
    # Locally the broker runs in Docker, published on 127.0.0.1:6380
    # (6379 on the host is already used by the Langfuse redis in docker-compose).
    # In compose the api/worker services override these to redis://redis-broker:6379/0.
    celery_broker_url: str = "redis://localhost:6380/0"
    celery_result_backend: str = "redis://localhost:6380/0"
    celery_task_result_expires: int = 86400
    celery_task_soft_time_limit: int = 2400
    celery_task_time_limit: int = 2700

    _mcp_servers: dict[str, MCPServerConfig] = PrivateAttr()

    def __init__(self, **data: Any) -> None:
        # The per-server classes share the parent's .env handling,
        # including _env_file=None in tests (deterministic, no .env).
        # The unpacked dict keeps this mypy-clean: the pydantic plugin's
        # generated __init__ signatures don't advertise _env_file.
        env_file = data.get("_env_file", ".env")
        super().__init__(**data)
        env: dict[str, Any] = {"_env_file": env_file}
        self._mcp_servers = {
            "rag_default": RagDefaultMCPServer(**env).as_entry(),
            "rag_iso20022": RagISO20022MCPServer(**env).as_entry(),
            "rag_iso8583": RagISO8583MCPServer(**env).as_entry(),
            "jira": JiraMCPServer(**env).as_entry(),
            "confluence": ConfluenceMCPServer(**env).as_entry(),
        }

    @property
    def mcp_servers(self) -> dict[str, MCPServerConfig]:
        """Registry of every MCP server the workflow may call."""
        return self._mcp_servers


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings (cached). Tests build ``Settings`` directly."""
    return Settings()
