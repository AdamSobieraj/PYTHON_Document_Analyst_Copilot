"""JSON extraction from LLM replies: bare, fenced, embedded, list — and failure cases."""

from __future__ import annotations

import pytest

from iso_insight.adapters.llm.openai_compatible import _extract_json
from iso_insight.exceptions import LLMError


def test_bare_object() -> None:
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_fenced_object_with_language() -> None:
    assert _extract_json('```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}


def test_fenced_object_without_language() -> None:
    assert _extract_json('```\n{"verdict": "pass"}\n```') == {"verdict": "pass"}


def test_object_embedded_in_prose() -> None:
    text = 'Oto odpowiedź: {"answer": "tak", "confidence": 0.8} — koniec.'
    assert _extract_json(text) == {"answer": "tak", "confidence": 0.8}


def test_bare_list_is_wrapped_as_items() -> None:
    assert _extract_json("[1, 2, 3]") == {"items": [1, 2, 3]}


def test_unparseable_reply_raises_llm_error() -> None:
    with pytest.raises(LLMError):
        _extract_json("brak jakiegokolwiek JSON w tej odpowiedzi")


def test_empty_reply_raises_llm_error() -> None:
    with pytest.raises(LLMError):
        _extract_json("")
