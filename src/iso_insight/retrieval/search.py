"""Retrieval: query the configured MCP servers and normalise the results.

This is the domain's view of "search the knowledge bases" — the actual
MCP transport lives in the tool-gateway adapter. Here we only shape
call arguments, pick which servers a query needs, and normalise
heterogeneous payloads into :class:`SourceRef` items.
"""

from __future__ import annotations

import re
from typing import Any

from iso_insight.config import DEFAULT_RAG_SERVER, RAG_STANDARD_MAP, Settings
from iso_insight.ports.tools import ToolGateway
from iso_insight.schemas.workflow import SourceRef, WorkflowState

# The native RAG server (PYTHON-Agent-MCP) returns its hits as one Polish
# text dump, one block per document:
#
#   --- DOKUMENT 1 (Relewancja: 0.5943) ---
#   Tytuł: plik.pdf
#   Źródło (plik): s3://bucket/general/plik.pdf
#   URL: s3://bucket/general/plik.pdf
#   Domena: general
#   Typ: .pdf
#   Tagi: s3, general
#   ID Fragmentu: <hex>
#   Treść:
#   <chunk text, until the next header>
#
# FastMCP mirrors that string in structured_content ({"result": <dump>}),
# and the gateway prefers structured content over text blocks — so the dump
# has to be parsed back into per-document items here.
_DUMP_HEADER = re.compile(
    r"^--- DOKUMENT (\d+)(?: \(Relewancja: ([0-9]+(?:\.[0-9]+)?)\))? ---[ \t]*$",
    re.MULTILINE,
)
_CONTENT_FIELD = re.compile(r"^\s*Treść\s*:[ \t]*", re.MULTILINE)


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


def _parse_document_dump(text: str) -> list[dict[str, Any]]:
    """The native RAG server's document dump → per-document items.

    Returns [] when the text is not a dump (no DOKUMENT headers) so callers
    can fall back to plain-text handling. Each item carries the keys
    :func:`_normalize_sources` already understands.
    """
    headers = list(_DUMP_HEADER.finditer(text))
    if not headers:
        return []
    items: list[dict[str, Any]] = []
    for position, header in enumerate(headers):
        end = headers[position + 1].start() if position + 1 < len(headers) else len(text)
        section = text[header.end():end]
        marker = _CONTENT_FIELD.search(section)
        if marker:
            header_block, content = section[: marker.start()], section[marker.end():]
        else:
            header_block, content = section, ""
        fields: dict[str, str] = {}
        for line in header_block.splitlines():
            if not line.strip() or ":" not in line:
                continue
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
        items.append(
            {
                "source_id": fields.get("ID Fragmentu") or f"document-{header.group(1)}",
                "title": fields.get("Tytuł") or f"Dokument {header.group(1)}",
                "url": fields.get("URL") or fields.get("Źródło (plik)"),
                "section": fields.get("Domena") or None,
                "quote": content.strip(),
                "score": _to_float(header.group(2)),
            }
        )
    return items


def _extract_items(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, str):
        parsed = _parse_document_dump(payload)
        return parsed if parsed else [payload]
    if isinstance(payload, dict):
        for key in ("results", "items", "documents", "hits", "sources"):
            items = payload.get(key)
            if isinstance(items, list):
                return items
        # The native server wraps the dump in a single string field —
        # structured_content {"result": ...} or the text-block fallback {"text": ...}.
        for key in ("result", "text"):
            value = payload.get(key)
            if isinstance(value, str):
                parsed = _parse_document_dump(value)
                if parsed:
                    return parsed
        return [payload]
    return [payload]


def _normalize_sources(payload: Any, source_type: str) -> list[SourceRef]:
    """Heterogeneous MCP payloads → a list of :class:`SourceRef` (quote-bearing items only)."""
    sources: list[SourceRef] = []
    for index, item in enumerate(_extract_items(payload), start=1):
        if not isinstance(item, dict):
            item = {"quote": str(item)}
        # "result" last: the generic wrapper some servers use around a plain
        # answer string (e.g. FastMCP structured_content {"result": ...}).
        quote = str(
            item.get("quote")
            or item.get("snippet")
            or item.get("text")
            or item.get("body")
            or item.get("result")
            or ""
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


def _rag_arguments(state: WorkflowState, cfg_top_k: int, collection: str | None) -> dict[str, Any]:
    """The RAG tool's argument contract: only what the knowledge-base tool accepts.

    The native MCP server validates strictly (additionalProperties: false),
    so no standards/user/permissions block — collection selection is how a
    query is scoped to a standard's knowledge base.
    """
    arguments: dict[str, Any] = {
        "query": state.get("retrieval_query") or state.get("query", ""),
        "top_k": cfg_top_k,
    }
    if collection:
        arguments["collection_name"] = collection
    return arguments


def _wiki_arguments(state: WorkflowState, cfg_top_k: int) -> dict[str, Any]:
    """The wiki tools' (Jira/Confluence) argument block; the limit is "max_results"."""
    return {
        "query": state.get("retrieval_query") or state.get("query", ""),
        "standards": state.get("detected_standards", []),
        "user_id": state.get("user_id"),
        "permissions": state.get("permissions", []),
        "max_results": cfg_top_k,
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
    arguments = _rag_arguments(state, cfg.top_k, cfg.collection)
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
    arguments = _wiki_arguments(state, cfg.top_k)
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
    arguments = _wiki_arguments(state, cfg.top_k)
    payload = await tools.call(
        server="confluence",
        tool=cfg.tool,
        arguments=arguments,
        timeout=cfg.timeout,
    )
    return _normalize_sources(payload, "confluence")
