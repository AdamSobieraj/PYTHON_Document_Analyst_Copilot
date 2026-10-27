"""Unit tests for the settings layer (env-only configuration)."""

import pytest

from iso_insight.config import DEFAULT_RAG_SERVER, RAG_STANDARD_MAP, Settings, get_settings


def _settings() -> Settings:
    # _env_file=None fully disables the repo's .env: deterministic defaults.
    return Settings(_env_file=None)


def test_defaults() -> None:
    s = _settings()
    assert s.max_iterations == 3
    assert s.low_confidence_threshold == 0.6
    assert s.llm_model == "bank-llm"
    assert s.llm_base_url == ""
    assert s.llm_temperature == 0.1
    assert s.llm_max_tokens == ""
    assert s.celery_broker_url == "redis://localhost:6380/0"
    assert s.celery_result_backend == "redis://localhost:6380/0"
    assert s.celery_task_result_expires == 86400


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("MAX_ITERATIONS", "7")
    s = _settings()
    assert s.llm_model == "test-model"
    assert s.max_iterations == 7


def test_mcp_registry() -> None:
    s = _settings()
    assert set(s.mcp_servers) == {
        "rag_default",
        "rag_iso20022",
        "rag_iso8583",
        "jira",
        "confluence",
    }
    rag = s.mcp_servers["rag_default"]
    assert rag.transport == "streamable_http"
    assert rag.url == "http://rag-default:8000/mcp"
    assert rag.tool == "rag_search"
    assert rag.top_k == 5
    assert rag.headers == {}


def test_mcp_token_becomes_bearer_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_MCP_TOKEN", "secret-token")
    s = _settings()
    assert s.mcp_servers["jira"].headers == {"Authorization": "Bearer secret-token"}


def test_standard_map() -> None:
    assert DEFAULT_RAG_SERVER == "rag_default"
    assert RAG_STANDARD_MAP == {"ISO20022": "rag_iso20022", "ISO8583": "rag_iso8583"}


def test_get_settings_cached() -> None:
    assert get_settings() is get_settings()
