"""In-memory LLMClient fake: returns recorded/golden responses per prompt.

The prompt name is resolved from the system message by matching the
versioned prompt file text (CLAUDE.md: tests use a fake LLM with golden
responses). Failures surface as the domain exceptions exactly like the
real adapter: :class:`TransientLLMError` for retryable, ``LLMError`` for
permanent ones.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from iso_insight.exceptions import LLMError, TransientLLMError
from iso_insight.llm.prompts import load_prompt
from iso_insight.ports.llm import LLMClient
from iso_insight.schemas.llm import LLMMessage


class FakeLLM(LLMClient):
    """Recorded responses keyed by prompt name (``iso_parser``, ``reflector``, ...).

    ``sequences`` lets one prompt return a different response on each call
    (the last item repeats). A configured ``Exception`` instance is raised
    in place of a response. ``fail_all`` makes every call raise a transient
    error, the way a downed LLM endpoint would.
    """

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        *,
        sequences: dict[str, list[Any]] | None = None,
        fail_all: bool = False,
    ) -> None:
        self._responses = dict(responses or {})
        self._sequences = {name: list(items) for name, items in (sequences or {}).items()}
        self._fail_all = fail_all
        self.calls: list[dict[str, Any]] = []

    @property
    def json_calls(self) -> list[dict[str, Any]]:
        """Only the structured-output (``json``) calls, in order."""
        return [call for call in self.calls if call["method"] == "json"]

    def _prompt_names(self) -> list[str]:
        names = list(self._responses)
        for name in self._sequences:
            if name not in names:
                names.append(name)
        return names

    def _prompt_name(self, system_prompt: str) -> str:
        for name in self._prompt_names():
            if load_prompt(name).text in system_prompt:
                return name
        raise LLMError("FakeLLM: system prompt nie pasuje do żadnego znanego promptu.")

    def _next(self, name: str) -> Any:
        items = self._sequences.get(name)
        if items:
            if len(items) > 1:
                return items.pop(0)
            return items[0]
        if name in self._responses:
            return self._responses[name]
        raise LLMError(f"FakeLLM: brak nagranej odpowiedzi dla promptu {name!r}.")

    async def chat(
        self,
        messages: Sequence[LLMMessage],
        *,
        temperature: float | None = None,
    ) -> str:
        self.calls.append(
            {"method": "chat", "messages": list(messages), "temperature": temperature}
        )
        if self._fail_all:
            raise TransientLLMError("FakeLLM: fail_all")
        return "Odpowiedź z FakeLLM."

    async def json(
        self,
        system_prompt: str,
        user_payload: str,
        *,
        temperature: float | None = None,
    ) -> Any:
        if self._fail_all:
            # The endpoint is down — record the call, then fail transiently,
            # the way a real adapter would (prompt name is unresolvable).
            self.calls.append(
                {
                    "method": "json",
                    "prompt": "unknown",
                    "payload": user_payload,
                    "temperature": temperature,
                }
            )
            raise TransientLLMError("FakeLLM: fail_all")
        name = self._prompt_name(system_prompt)
        self.calls.append(
            {
                "method": "json",
                "prompt": name,
                "payload": user_payload,
                "temperature": temperature,
            }
        )
        response = self._next(name)
        if isinstance(response, Exception):
            raise response
        return response
