"""Port: LLM client (OpenAI-compatible chat completions).

The domain calls the model exclusively through this interface; the
concrete client is chosen in the composition root (config). Structural
facts (page numbers, positions) never come from the LLM — only from
deterministic parser metadata.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from iso_insight.schemas.llm import LLMMessage


class LLMClient(ABC):
    """Async LLM client.

    Raises :class:`~iso_insight.exceptions.LLMError` on failure,
    :class:`~iso_insight.exceptions.TransientLLMError` when the failure
    is retryable (429 / 5xx / timeout).
    """

    @abstractmethod
    async def chat(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float | None = None,
    ) -> str:
        """One chat-completion call; returns the raw text of the reply."""

    @abstractmethod
    async def json(
        self,
        system_prompt: str,
        user_payload: str,
        *,
        temperature: float | None = None,
    ) -> Any:
        """A chat call whose reply must be JSON; parsed (dict/list) and returned."""
