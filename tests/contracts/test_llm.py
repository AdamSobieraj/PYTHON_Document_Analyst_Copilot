"""LLM client port contract: fake and OpenAI-compatible adapter.

The real adapter is exercised *unconfigured* (base URL forced empty) —
the contract is about the interface shape (parsed JSON on success, the
domain ``LLMError`` on failure), never about a live endpoint. Real calls
belong to ``-m live`` tests only (CLAUDE.md).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from iso_insight.adapters.llm.openai_compatible import OpenAICompatibleLLM
from iso_insight.adapters.tracing.noop import NoOpTracing
from iso_insight.config import Settings
from iso_insight.exceptions import LLMError, TransientError, TransientLLMError
from iso_insight.llm.prompts import load_prompt
from iso_insight.ports.llm import LLMClient
from iso_insight.schemas.llm import LLMMessage
from tests.fakes.llm import FakeLLM


def _unconfigured_settings() -> Settings:
    # Endpoint forced empty regardless of the ambient environment: the
    # contract suite must stay offline.
    return Settings(llm_base_url="", llm_api_key="", _env_file=None)


@pytest.fixture(params=["fake", "openai"])
def llm(request: pytest.FixtureRequest, golden_responses: dict[str, Any]) -> LLMClient:
    if request.param == "fake":
        return FakeLLM(responses=dict(golden_responses))
    return OpenAICompatibleLLM(_unconfigured_settings(), NoOpTracing())


async def test_json_returns_parsed_json_or_llm_error(llm: LLMClient) -> None:
    payload = json.dumps({"query": "ISO 20022 pacs.002"})
    try:
        result: Any = await llm.json(load_prompt("iso_parser").text, payload)
    except Exception as error:
        # Contract: only the domain exception may escape the adapter.
        assert isinstance(error, LLMError)
        return
    assert isinstance(result, (dict, list))


async def test_chat_returns_text_or_llm_error(llm: LLMClient) -> None:
    try:
        reply = await llm.chat([LLMMessage(role="user", content="Cześć")])
    except Exception as error:
        assert isinstance(error, LLMError)
        return
    assert isinstance(reply, str)


async def test_unconfigured_real_client_reports_permanent_failure() -> None:
    client = OpenAICompatibleLLM(_unconfigured_settings(), NoOpTracing())
    with pytest.raises(LLMError) as excinfo:
        await client.chat([LLMMessage(role="user", content="Cześć")])
    assert not isinstance(excinfo.value, TransientError)
    with pytest.raises(LLMError) as excinfo:
        await client.json(load_prompt("iso_parser").text, "{}")
    assert not isinstance(excinfo.value, TransientError)


async def test_real_adapter_close_is_safe() -> None:
    await OpenAICompatibleLLM(_unconfigured_settings(), NoOpTracing()).close()


async def test_fake_returns_the_recorded_golden_response(
    golden_responses: dict[str, Any],
) -> None:
    fake = FakeLLM(responses=dict(golden_responses))
    result: Any = await fake.json(load_prompt("draft_answer").text, "{}")
    assert result == golden_responses["draft_answer"]
    reply = await fake.chat([LLMMessage(role="user", content="Cześć")])
    assert reply == "Odpowiedź z FakeLLM."


async def test_fake_fail_all_raises_transient_llm_error() -> None:
    fake = FakeLLM(fail_all=True)
    with pytest.raises(TransientLLMError):
        await fake.chat([LLMMessage(role="user", content="Cześć")])
    with pytest.raises(TransientLLMError):
        await fake.json(load_prompt("reflector").text, "{}")
