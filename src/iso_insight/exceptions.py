"""Domain exceptions.

Ports raise these; adapters translate vendor/library exceptions into
them. Retry logic in workers keys off :class:`TransientError` (429,
timeouts, broker blips) — never off a specific library exception.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all domain errors."""


class TransientError(DomainError):
    """A transient failure — safe to retry with exponential backoff + jitter."""


class LLMError(DomainError):
    """The LLM call failed (non-2xx, timeout, unparseable response...)."""


class TransientLLMError(TransientError, LLMError):
    """A retryable LLM failure (429 / 5xx / timeout)."""


class ToolCallError(DomainError):
    """A tool call on an MCP server failed."""


class TransientToolCallError(TransientError, ToolCallError):
    """A retryable tool-call failure (timeout, 429, transport blip)."""


class QueueError(DomainError):
    """The task queue rejected the operation (no worker, unknown task...)."""


class TransientQueueError(TransientError, QueueError):
    """A retryable queue failure (broker / result backend unreachable)."""


class ArtifactNotFound(DomainError):
    """A requested artifact (stored result, document) does not exist."""


class RepositoryError(DomainError):
    """A persistent (non-transient) repository/storage failure."""
