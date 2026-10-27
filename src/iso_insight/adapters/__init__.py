"""Adapters — concrete implementations of the ports.

The only part of the codebase allowed to import infrastructure
libraries (httpx, mcp, langfuse, celery, ...). One subpackage per
concern: ``llm/``, ``tracing/``, ``tools/``, ``queue/``.
"""
