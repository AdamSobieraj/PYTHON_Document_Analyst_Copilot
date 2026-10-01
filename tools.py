"""MCP tool wrappers: RAG / Jira / Confluence search.

Each search function builds the tool arguments for one MCP server, calls
the tool through the shared MCPManager and normalizes the raw payload
into a list of SourceRef evidence items. Items without a usable quote
are dropped — the evidence validator downstream relies on quotes.
Failures propagate to the caller (the retrieval node), which records
them as audit events and continues with whatever it retrieved.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from state import WorkflowState, SourceRef
from config import MCP_SERVERS, DEFAULT_RAG_SERVER
from mcp_client import mcp_manager


def _to_float(value: Any) -> float:
    """Coerce to float, returning 0.0 for anything unparsable."""
    try:
        return float(value)
    except Exception:
        return 0.0


def _to_optional_str(value: Any) -> Optional[str]:
    """Coerce to str or None — the API response schema is strict about these fields."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _extract_items(payload: Any) -> List[Any]:
    """Flatten an MCP tool payload into a list of items.

    Handles lists, dicts with a known collection key
    (results/items/documents/hits/sources), and single values.
    """
    if payload is None:
        return []

    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        for key in ("results", "items", "documents", "hits", "sources"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return [payload]

    return [payload]


def _normalize_sources(payload: Any, source_type: str) -> List[SourceRef]:
    """Convert raw MCP items into SourceRef dicts.

    Accepts several field spellings (quote/snippet/text/body, id,
    name/title, link/url, path/section) so different MCP services can be
    plugged in without code changes.
    """
    sources: List[SourceRef] = []

    for index, item in enumerate(_extract_items(payload), start=1):
        if not isinstance(item, dict):
            item = {"quote": str(item)}

        quote = str(
            item.get("quote")
            or item.get("snippet")
            or item.get("text")
            or item.get("body")
            or ""
        ).strip()

        # Evidence without a quote cannot ground an answer — skip it.
        if not quote:
            continue

        sources.append(
            {
                "source_type": str(item.get("source_type") or source_type),
                "source_id": str(
                    item.get("source_id")
                    or item.get("id")
                    or f"{source_type}-{index}"
                ),
                "title": str(
                    item.get("title")
                    or item.get("name")
                    or f"{source_type} source {index}"
                ),
                "url": _to_optional_str(item.get("url") or item.get("link")),
                "section": _to_optional_str(item.get("section") or item.get("path")),
                "quote": quote,
                "score": _to_float(
                    item.get("score") or item.get("relevance") or 0.0
                ),
            }
        )

    return sources


async def rag_search(
    state: WorkflowState,
    server_name: str = DEFAULT_RAG_SERVER,
) -> List[SourceRef]:
    """Search one RAG knowledge base (per-ISO routing is done by the caller).

    Sends the current retrieval query (falling back to the original
    question), the detected ISO standards and the caller's user_id /
    permissions, limited to the server's configured top_k. An unknown
    server name returns [] without raising; transport/tool failures
    propagate (MCPError, timeout) and are converted into "mcp_tool_error"
    audit events by the retrieval node in nodes.py.
    """
    if server_name not in MCP_SERVERS:
        return []

    cfg = MCP_SERVERS[server_name]

    arguments: Dict[str, Any] = {
        "query": state.get("retrieval_query") or state.get("query", ""),
        "standards": state.get("detected_standards", []),
        "user_id": state.get("user_id"),
        "permissions": state.get("permissions", []),
        "top_k": cfg.get("top_k", 5),
    }

    payload = await mcp_manager.call_tool(
        server=server_name,
        tool=cfg["tool"],
        arguments=arguments,
        timeout=cfg.get("timeout"),
    )

    return _normalize_sources(payload, server_name)


async def jira_search(state: WorkflowState) -> List[SourceRef]:
    """Search related Jira tickets via the Jira MCP service (read-only).

    Same argument shape as rag_search (query, standards, caller identity,
    permissions), capped at the service's configured top_k. Never writes
    to Jira — the tool call is a search only. Failures propagate to the
    retrieval node, which records an audit event and continues with the
    other sources.
    """
    cfg = MCP_SERVERS["jira"]

    arguments: Dict[str, Any] = {
        "query": state.get("retrieval_query") or state.get("query", ""),
        "standards": state.get("detected_standards", []),
        "user_id": state.get("user_id"),
        "permissions": state.get("permissions", []),
        "max_results": cfg.get("top_k", 5),
    }

    payload = await mcp_manager.call_tool(
        server="jira",
        tool=cfg["tool"],
        arguments=arguments,
        timeout=cfg.get("timeout"),
    )

    return _normalize_sources(payload, "jira")


async def confluence_search(state: WorkflowState) -> List[SourceRef]:
    """Search related Confluence pages via the Confluence MCP service (read-only).

    Same argument shape as rag_search (query, standards, caller identity,
    permissions), capped at the service's configured top_k. Never writes
    to Confluence — the tool call is a search only. Failures propagate to
    the retrieval node, which records an audit event and continues with
    the other sources.
    """
    cfg = MCP_SERVERS["confluence"]

    arguments: Dict[str, Any] = {
        "query": state.get("retrieval_query") or state.get("query", ""),
        "standards": state.get("detected_standards", []),
        "user_id": state.get("user_id"),
        "permissions": state.get("permissions", []),
        "max_results": cfg.get("top_k", 5),
    }

    payload = await mcp_manager.call_tool(
        server="confluence",
        tool=cfg["tool"],
        arguments=arguments,
        timeout=cfg.get("timeout"),
    )

    return _normalize_sources(payload, "confluence")
