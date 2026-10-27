"""Tracing port contract: fake, no-op and Langfuse adapters share the guarantees.

The workflow must never see a tracing failure, and tracing must stay a
cheap identity when disabled (CLAUDE.md: one contract suite per port,
parameterized over every implementation).
"""

from __future__ import annotations

import pytest

from iso_insight.adapters.tracing.langfuse import LangfuseTracing
from iso_insight.adapters.tracing.noop import NoOpTracing
from iso_insight.config import Settings
from iso_insight.ports.tracing import Tracing
from tests.fakes.tracing import FakeTracing


def _unconfigured_settings() -> Settings:
    # Langfuse keys forced empty regardless of the ambient environment:
    # the contract suite must stay offline (no Langfuse network calls).
    return Settings(
        langfuse_host="",
        langfuse_public_key="",
        langfuse_secret_key="",
        _env_file=None,
    )


@pytest.fixture(params=["fake", "noop", "langfuse"])
def tracing(request: pytest.FixtureRequest) -> Tracing:
    if request.param == "fake":
        return FakeTracing()
    if request.param == "noop":
        return NoOpTracing()
    return LangfuseTracing(_unconfigured_settings())


def test_enabled_is_a_bool(tracing: Tracing) -> None:
    assert isinstance(tracing.enabled, bool)


def test_observe_keeps_sync_results(tracing: Tracing) -> None:
    def add(left: int, right: int) -> int:
        return left + right

    traced = tracing.observe(name="contract.add", as_type="span")(add)
    assert traced(21, 21) == 42


async def test_observe_keeps_async_results(tracing: Tracing) -> None:
    async def add(left: int, right: int) -> int:
        return left + right

    traced = tracing.observe(name="contract.add_async", as_type="generation")(add)
    assert await traced(21, 21) == 42


def test_observe_passes_arguments_through(tracing: Tracing) -> None:
    def build(tag: str, count: int = 1) -> str:
        return tag * count

    traced = tracing.observe(name="contract.build")(build)
    assert traced("ab", 2) == "abab"
    assert traced("ab") == "ab"


def test_trace_attributes_is_a_working_context_manager(tracing: Tracing) -> None:
    inside = False
    with tracing.trace_attributes(user_id="tester", tags=["contract"]):
        inside = True
    assert inside


def test_generation_details_and_flush_never_raise(tracing: Tracing) -> None:
    tracing.set_generation_details(
        model="contract-model",
        model_parameters={"temperature": 0.1},
        usage_details={"input_tokens": 10, "output_tokens": 5},
    )
    tracing.flush()


def test_langfuse_is_disabled_without_credentials() -> None:
    # With empty keys the real adapter must report itself as off — no
    # network, no side effects, the workflow falls back to no-op tracing.
    assert LangfuseTracing(_unconfigured_settings()).enabled is False
