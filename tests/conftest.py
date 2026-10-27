"""Shared fixtures: deterministic settings, golden LLM responses, canned tool payloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from iso_insight.config import Settings
from tests.fakes.llm import FakeLLM
from tests.fakes.tools import FakeToolGateway
from tests.fakes.tracing import FakeTracing

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings() -> Settings:
    """Settings built without .env — deterministic, no local secrets."""
    return Settings(_env_file=None)


@pytest.fixture
def golden_responses() -> dict[str, Any]:
    raw = (FIXTURES_DIR / "golden_responses.json").read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    return data


@pytest.fixture
def fake_llm(golden_responses: dict[str, Any]) -> FakeLLM:
    return FakeLLM(responses=dict(golden_responses))


@pytest.fixture
def fake_tools() -> FakeToolGateway:
    """Four quote-bearing sources: 2 from the default RAG, 1 from ISO 20022, 1 from Jira."""
    return FakeToolGateway(
        sessions={
            "rag_default": [
                {
                    "quote": "ISO 20022 wymaga walidacji schematów XML wiadomości bankowych.",
                    "id": "rag-1",
                    "title": "Specyfikacja ISO 20022",
                    "score": 0.9,
                },
                {
                    "quote": "Wiadomość pacs.002.001.08 przenosi polecenia płatności SEPA.",
                    "id": "rag-2",
                    "title": "Przewodnik pacs.002",
                    "score": 0.8,
                },
            ],
            "rag_iso20022": [
                {
                    "quote": "Wersja 2013 standardu ISO 20022 rozszerza pola o dane klienta.",
                    "id": "iso20022-1",
                    "title": "ISO 20022 v2013",
                    "score": 0.85,
                }
            ],
            "jira": {
                "results": [
                    {
                        "quote": "TICKET-42: migracja kanału SEPA na ISO 20022.",
                        "id": "TICKET-42",
                        "title": "TICKET-42",
                    }
                ]
            },
            "confluence": [],
        }
    )


@pytest.fixture
def fake_tracing() -> FakeTracing:
    return FakeTracing()
