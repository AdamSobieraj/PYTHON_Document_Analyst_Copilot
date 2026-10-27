"""Retrieval: query the configured MCP servers and normalise the results.

This is the domain's view of "search the knowledge bases" — the actual
MCP transport lives in the tool-gateway adapter. Here we only shape
call arguments, pick which servers a query needs, and normalise
heterogeneous payloads into :class:`SourceRef` items.
"""

from __future__ import annotations

from typing import Any

from iso_insight.config import DEFAULT_RAG_SERVER, RAG_STANDARD_MAP, Settings
from iso_insight.ports.tools import ToolGateway
from iso_insight.schemas.workflow import SourceRef, WorkflowState


def select_rag_servers(detected_standards: list[str], settings: Settings) -> list[str]:
    """Which RAG servers a query should hit: the default one plus any standard-specific bases.

    Unknown standards are ignored; servers that are not configured
    (``Settings.mcp_servers``) are dropped.
    """
    servers = [DEFAULT_RAG_SERVER]
    for standard in detected_standards:
        name = RAG_STANDARD_MAP.get(standard)
        if name and name not in servers:
            servers.append(name)
    return [name for name in servers if name in settings.mcp_servers]


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _extract_items(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "items", "documents", "hits", "sources"):
            items = payload.get(key)
            if isinstance(items, list):
                return items
        return [payload]
    return [payload]


def _normalize_sources(payload: Any, source_type: str) -> list[SourceRef]:
    """Heterogeneous MCP payloads → a list of :class:`SourceRef` (quote-bearing items only)."""
    sources: list[SourceRef] = []
    for index, item in enumerate(_extract_items(payload), start=1):
        if not isinstance(item, dict):
            item = {"quote": str(item)}
        quote = str(
            item.get("quote") or item.get("snippet") or item.get("text") or item.get("body") or ""
        ).strip()
        if not quote:
            continue
        sources.append(
            {
                "source_type": str(item.get("source_type") or source_type),
                "source_id": str(
                    item.get("source_id") or item.get("id") or f"{source_type}-{index}"
                ),
                "title": str(
                    item.get("title") or item.get("name") or f"{source_type} source {index}"
                ),
                "url": _to_optional_str(item.get("url") or item.get("link")),
                "section": _to_optional_str(item.get("section") or item.get("path")),
                "quote": quote,
                "score": _to_float(item.get("score") or item.get("relevance") or 0.0),
            }
        )
    return sources


def _search_arguments(state: WorkflowState, cfg_top_k: int, *, results_key: str) -> dict[str, Any]:
    """The common argument block; RAG servers call it "top_k", the wiki tools "max_results"."""
    return {
        "query": state.get("retrieval_query") or state.get("query", ""),
        "standards": state.get("detected_standards", []),
        "user_id": state.get("user_id"),
        "permissions": state.get("permissions", []),
        results_key: cfg_top_k,
    }


async def rag_search(
    state: WorkflowState,
    tools: ToolGateway,
    settings: Settings,
    server_name: str = DEFAULT_RAG_SERVER,
) -> list[SourceRef]:
    """Search one RAG server; an unknown server name yields no sources (graceful degradation)."""
    cfg = settings.mcp_servers.get(server_name)
    if cfg is None:
        return []
    arguments = _search_arguments(state, cfg.top_k, results_key="top_k")
    payload = await tools.call(
        server=server_name,
        tool=cfg.tool,
        arguments=arguments,
        timeout=cfg.timeout,
    )
    return _normalize_sources(payload, server_name)


async def jira_search(
    state: WorkflowState, tools: ToolGateway, settings: Settings
) -> list[SourceRef]:
    """Search the Jira MCP server for related tickets."""
    cfg = settings.mcp_servers.get("jira")
    if cfg is None:
        return []
    arguments = _search_arguments(state, cfg.top_k, results_key="max_results")
    payload = await tools.call(
        server="jira", tool=cfg.tool, arguments=arguments, timeout=cfg.timeout
    )
    return _normalize_sources(payload, "jira")


async def confluence_search(
    state: WorkflowState, tools: ToolGateway, settings: Settings
) -> list[SourceRef]:
    """Search the Confluence MCP server for related pages."""
    cfg = settings.mcp_servers.get("confluence")
    if cfg is None:
        return []
    arguments = _search_arguments(state, cfg.top_k, results_key="max_results")
    payload = await tools.call(
        server="confluence",
        tool=cfg.tool,
        arguments=arguments,
        timeout=cfg.timeout,
    )
    return _normalize_sources(payload, "confluence")
