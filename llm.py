"""Minimal OpenAI-compatible LLM client.

llm_chat() performs one chat-completion call and returns the raw text.
llm_json() wraps that with a system prompt that forces JSON output and
extracts the JSON object from the reply, tolerating common LLM output
quirks (markdown code fences, surrounding prose, JSON arrays).
All failures raise LLMError; callers (the agent nodes) catch it and
degrade gracefully.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

import httpx

from config import (
    LLM_BASE_URL,
    LLM_API_KEY,
    LLM_MODEL,
    LLM_TIMEOUT,
    LLM_TEMPERATURE,
    LLM_MAX_TOKENS,
)
from tracing import observe as langfuse_observe, set_generation_details


class LLMError(RuntimeError):
    """Raised when the LLM call fails or its response cannot be parsed."""


def _headers() -> Dict[str, str]:
    """Content-Type header plus a Bearer token when an API key is configured."""
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    return headers


def _extract_json(text: str) -> Dict[str, Any]:
    """Parse a JSON object out of an LLM reply.

    Tolerates markdown code fences and prose around the JSON. A top-level
    array is wrapped into {"items": [...]} so callers can rely on a dict.
    Raises LLMError when no JSON object can be recovered.
    """
    cleaned = text.strip()

    # Strip optional ```json / ``` code fences.
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
        return {"items": data}
    except json.JSONDecodeError:
        # Fallback: take the first {...} block inside the reply.
        start = cleaned.find("{")
        end = cleaned.rfind("}")

        if start != -1 and end != -1 and end > start:
            data = json.loads(cleaned[start : end + 1])
            if isinstance(data, dict):
                return data
            return {"items": data}

        raise LLMError("Nie udało się sparsować JSON z odpowiedzi LLM.")


@langfuse_observe(name="llm.chat", as_type="generation")
async def llm_chat(
    messages: List[Dict[str, str]],
    temperature: Optional[float] = None,
) -> str:
    """One chat-completion call against {LLM_BASE_URL}/chat/completions.

    Returns the assistant message text. Raises LLMError on HTTP errors,
    missing base URL or an unexpected response shape.

    Tracing: recorded as a Langfuse generation with the model name,
    sampling parameters and token usage (no-op when tracing is off).
    """
    if not LLM_BASE_URL:
        raise LLMError("Brak LLM_BASE_URL.")

    payload: Dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": LLM_TEMPERATURE if temperature is None else temperature,
    }

    # Only send max_tokens when explicitly configured (some local models reject it).
    if LLM_MAX_TOKENS:
        try:
            payload["max_tokens"] = int(LLM_MAX_TOKENS)
        except Exception:
            pass

    url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"

    async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
        response = await client.post(
            url,
            headers=_headers(),
            json=payload,
        )

        if response.status_code >= 400:
            raise LLMError(
                f"LLM zwrócił błąd {response.status_code}: {response.text[:500]}"
            )

        data = response.json()

        try:
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise LLMError("Nieoczekiwany format odpowiedzi LLM.") from exc

        # Attach model/sampling/token data to the current generation
        # (silent no-op when tracing is disabled).
        raw_usage = data.get("usage") or {}
        usage_details = (
            {
                "input": int(raw_usage.get("prompt_tokens") or 0),
                "output": int(raw_usage.get("completion_tokens") or 0),
                "total": int(raw_usage.get("total_tokens") or 0),
            }
            if raw_usage
            else None
        )
        set_generation_details(
            model=LLM_MODEL,
            model_parameters={"temperature": payload["temperature"]},
            usage_details=usage_details,
        )

        return content


async def llm_json(
    system_prompt: str,
    user_payload: Any,
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    """Prompt the model for a strict-JSON object and parse the reply.

    The system prompt is augmented with a "JSON only" instruction; the
    user payload is JSON-serialized when it is not already a string.
    """
    user_text = (
        user_payload
        if isinstance(user_payload, str)
        else json.dumps(user_payload, ensure_ascii=False, indent=2)
    )

    messages = [
        {
            "role": "system",
            "content": (
                f"{system_prompt.strip()}\n\n"
                "Zwróć wyłącznie poprawny JSON. "
                "Nie dodawaj komentarzy, markdown ani tekstu poza JSON."
            ),
        },
        {
            "role": "user",
            "content": user_text,
        },
    ]

    content = await llm_chat(messages, temperature=temperature)
    return _extract_json(content)
