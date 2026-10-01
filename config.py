"""Central application configuration.

Loads environment variables (see .env / .env.example) and exposes:

- LLM endpoint settings (OpenAI-compatible chat completion API),
- the MCP server registry (RAG knowledge bases, Jira, Confluence),
- guardrail patterns (PII, prompt injection) used by input validation,
- workflow tuning knobs (iteration budget, confidence threshold),
- Celery / Redis broker settings for the async query queue (celery_app.py).
"""

from __future__ import annotations

import os
from typing import Any, Dict

from dotenv import load_dotenv

load_dotenv()


# Max reflector/improve loops before the workflow is forced to output.
MAX_ITERATIONS = 3
# Answers with lower confidence are escalated to a human analyst.
LOW_CONFIDENCE_THRESHOLD = 0.6


# --- LLM (OpenAI-compatible endpoint: {LLM_BASE_URL}/chat/completions) ---
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "bank-llm")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "60"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))
# Empty string means "do not send max_tokens" (model default applies).
LLM_MAX_TOKENS = os.getenv("LLM_MAX_TOKENS", "").strip()


# --- Celery / Redis broker (async queries) ---
# Locally the broker runs in Docker, published on 127.0.0.1:6380
# (6379 on the host is already used by the Langfuse redis in docker-compose).
# In compose the api/worker services override these to redis://redis-broker:6379/0.
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6380/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6380/0")
CELERY_TASK_RESULT_EXPIRES = int(os.getenv("CELERY_TASK_RESULT_EXPIRES", "86400"))
CELERY_TASK_SOFT_TIME_LIMIT = int(os.getenv("CELERY_TASK_SOFT_TIME_LIMIT", "2400"))
CELERY_TASK_TIME_LIMIT = int(os.getenv("CELERY_TASK_TIME_LIMIT", "2700"))


# Regex patterns treated as personal data — a matching query is blocked.
PII_PATTERNS = [
    r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b",
    r"\b\d{9}\b",
    r"\b\d{2}[-\s]?\d{2}[-\s]?\d{2}[-\s]?\d{2}\b",
]


# Lower-cased substrings that indicate a prompt-injection attempt.
INJECTION_KEYWORDS = [
    "ignore previous",
    "ignore all",
    "system prompt",
    "prompt injection",
    "jailbreak",
    "bypass",
    "wygeneruj hasło",
    "pokaż hasło",
    "pomiń zabezpieczenia",
]


def _optional_headers(token_env: str) -> Dict[str, str]:
    """Build an Authorization header from a token env var, if the token is set."""
    token = os.getenv(token_env, "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _rag_server(prefix: str, default_url: str) -> Dict[str, Any]:
    """Read one RAG MCP server entry from the environment (<prefix>_TRANSPORT/_URL/...)."""
    return {
        "transport": os.getenv(f"{prefix}_TRANSPORT", "streamable_http"),
        "url": os.getenv(f"{prefix}_URL", default_url),
        "headers": _optional_headers(f"{prefix}_TOKEN"),
        "tool": os.getenv(f"{prefix}_TOOL", "rag_search"),
        "timeout": float(os.getenv(f"{prefix}_TIMEOUT", "30")),
        "top_k": int(os.getenv(f"{prefix}_TOP_K", "5")),
    }


def _service(
    prefix: str,
    default_url: str,
    default_tool: str,
) -> Dict[str, Any]:
    """Read one generic MCP service entry from the environment (Jira, Confluence)."""
    return {
        "transport": os.getenv(f"{prefix}_TRANSPORT", "streamable_http"),
        "url": os.getenv(f"{prefix}_URL", default_url),
        "headers": _optional_headers(f"{prefix}_TOKEN"),
        "tool": os.getenv(f"{prefix}_TOOL", default_tool),
        "timeout": float(os.getenv(f"{prefix}_TIMEOUT", "30")),
        "top_k": int(os.getenv(f"{prefix}_TOP_K", "5")),
    }


# Registry of every MCP server the workflow may call.
MCP_SERVERS = {
    "rag_default": _rag_server(
        "RAG_DEFAULT_MCP",
        "http://rag-default:8000/mcp",
    ),
    "rag_iso20022": _rag_server(
        "RAG_ISO20022_MCP",
        "http://rag-iso20022:8000/mcp",
    ),
    "rag_iso8583": _rag_server(
        "RAG_ISO8583_MCP",
        "http://rag-iso8583:8000/mcp",
    ),
    "jira": _service(
        "JIRA_MCP",
        "http://jira-mcp-service:8000/mcp",
        "jira_search",
    ),
    "confluence": _service(
        "CONFLUENCE_MCP",
        "http://confluence-mcp-service:8000/mcp",
        "confluence_search",
    ),
}


# Fallback RAG base used when no ISO-specific knowledge base matches the query.
DEFAULT_RAG_SERVER = "rag_default"


# Maps a detected ISO standard to its dedicated RAG knowledge base.
RAG_STANDARD_MAP = {
    "ISO20022": "rag_iso20022",
    "ISO8583": "rag_iso8583",
    # "ISO15022": "rag_iso15022",
    # "ISO20022_FX": "rag_iso20022_fx",
}
