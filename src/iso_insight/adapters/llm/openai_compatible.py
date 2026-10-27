"""OpenAI-compatible chat-completions LLM adapter (raw httpx, no SDK).

Implements the :class:`~iso_insight.ports.llm.LLMClient` port against any
``/v1/chat/completions`` endpoint (the LLM gateway, LM Studio, ...).
Transient failures (timeouts, 429, 5xx) surface as
:class:`TransientLLMError` so callers can retry with backoff; permanent
failures as :class:`LLMError`. Each chat call is traced as a Langfuse
generation through the injected :class:`Tracing` port.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Any

import httpx

from iso_insight.config import Settings
from iso_insight.exceptions import LLMError, TransientLLMError
from iso_insight.pipeline.retry import retry_transient
from iso_insight.ports.llm import LLMClient
from iso_insight.ports.tracing import Tracing
from iso_insight.schemas.llm import LLMMessage

logger = logging.getLogger("iso_insight.llm")

_FENCE_RE = re.compile(r"^```[a-z]*\n?|\n?```$", re.IGNORECASE)

_JSON_SUFFIX = (
    "\n\nZwróć wyłącznie poprawny JSON. Nie dodawaj komentarzy, markdown ani tekstu poza JSON."
)


def _extract_json(text: str) -> Any:
    """Parse JSON out of an LLM reply: bare, fenced, or embedded in prose.

    A bare list is wrapped as ``{"items": [...]}`` so node code always
    receives a mapping. Raises :class:`LLMError` when no JSON object is
    recoverable.
    """
    candidate = _FENCE_RE.sub("", text.strip()).strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("Nie udało się sparsować JSON z odpowiedzi LLM.") from None
        try:
            data = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            raise LLMError("Nie udało się sparsować JSON z odpowiedzi LLM.") from None
    if isinstance(data, list):
        return {"items": data}
    return data


class OpenAICompatibleLLM(LLMClient):
    """Chat-completions client; the httpx connection is created lazily once."""

    def __init__(self, settings: Settings, tracing: Tracing) -> None:
        self._settings = settings
        self._tracing = tracing
        self._client: httpx.AsyncClient | None = None
        # The public chat() is traced as a generation; _chat_once is the
        # retried, untraced core so retries share one observation.
        self._traced_chat = tracing.observe(name="llm.chat", as_type="generation")(self._chat_once)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self._settings.llm_api_key}"
        return headers

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=self._headers(), timeout=httpx.Timeout(self._settings.llm_timeout)
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying connection (called from process teardown)."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def chat(
        self, messages: Sequence[LLMMessage], *, temperature: float | None = None
    ) -> str:
        text, _usage = await self._traced_chat(messages, temperature)
        return str(text)

    async def _chat_once(
        self, messages: Sequence[LLMMessage], temperature: float | None
    ) -> tuple[str, dict[str, int] | None]:
        """One chat round with retries, then generation details for tracing."""
        text, usage = await retry_transient(
            lambda: self._chat_http(messages, temperature),
            max_attempts=self._settings.llm_max_retries,
        )
        max_tokens = self._settings.llm_max_tokens
        model_parameters: dict[str, Any] = {
            "temperature": self._settings.llm_temperature if temperature is None else temperature
        }
        if max_tokens:
            try:
                model_parameters["max_tokens"] = int(max_tokens)
            except ValueError:
                pass
        self._tracing.set_generation_details(
            model=self._settings.llm_model,
            model_parameters=model_parameters,
            usage_details=usage or {},
        )
        return text, usage

    async def _chat_http(
        self, messages: Sequence[LLMMessage], temperature: float | None
    ) -> tuple[str, dict[str, int] | None]:
        """The single HTTP round-trip; maps vendor failures onto the domain exceptions."""
        base_url = self._settings.llm_base_url
        if not base_url:
            raise LLMError("LLM nie jest skonfigurowany (puste LLM_BASE_URL).")
        payload: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": [message.model_dump() for message in messages],
            "temperature": self._settings.llm_temperature if temperature is None else temperature,
        }
        if self._settings.llm_max_tokens:
            try:
                payload["max_tokens"] = int(self._settings.llm_max_tokens)
            except ValueError:
                pass
        url = f"{base_url.rstrip('/')}/chat/completions"
        try:
            response = await self._http().post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise TransientLLMError(f"LLM timeout: {exc.__class__.__name__}") from exc
        except httpx.HTTPError as exc:
            raise TransientLLMError(f"LLM connection error: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            if response.status_code == 429 or response.status_code >= 500:
                raise TransientLLMError(f"LLM zwrócił status {response.status_code}")
            raise LLMError(f"LLM zwrócił status {response.status_code}")
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError("Odpowiedź LLM nie zawiera treści.") from exc
        usage = _parse_usage(data.get("usage"))
        return str(text), usage

    async def json(
        self, system_prompt: str, user_payload: str, *, temperature: float | None = None
    ) -> Any:
        """Structured-output call: force a JSON reply and parse it."""
        user_text = (
            user_payload
            if isinstance(user_payload, str)
            else json.dumps(user_payload, ensure_ascii=False)
        )
        messages = (
            LLMMessage(role="system", content=system_prompt + _JSON_SUFFIX),
            LLMMessage(role="user", content=user_text),
        )
        text = await self.chat(messages, temperature=temperature)
        return _extract_json(text)


def _parse_usage(usage: Any) -> dict[str, int] | None:
    """Normalise the API usage block to Langfuse's input/output/total shape."""
    if not isinstance(usage, dict):
        return None
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    if prompt_tokens is None and completion_tokens is None and total_tokens is None:
        return None
    return {
        "input": int(prompt_tokens or 0),
        "output": int(completion_tokens or 0),
        "total": int(total_tokens or (int(prompt_tokens or 0) + int(completion_tokens or 0))),
    }
